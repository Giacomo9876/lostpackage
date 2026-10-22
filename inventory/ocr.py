"""Lettura del totale da una foto di scontrino (facoltativa).

Se sul computer c'è Tesseract (PythonAnywhere lo ha già) la foto dello scontrino di chiusura viene
letta e l'importo del totale compilato in automatico. È solo un aiuto: chi chiude la giornata vede
sempre l'importo letto, lo conferma o lo corregge, e `DayClose.detected_total` conserva ciò che era
stato letto. Senza Tesseract (o se non riesce a leggere nulla) si scrive l'importo a mano e la foto
resta allegata come prova: nessuna parte dell'app dipende dalla lettura.

Sicurezza: Tesseract parte con argomenti fissi (nessuna shell), su un file temporaneo creato qui
da un'immagine già ricodificata, con un limite di tempo.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core import signing
from PIL import Image, ImageOps

TIMEOUT_SECONDS = 25
TOKEN_SALT = "inventory.receipt-reading"
TOKEN_MAX_AGE = 30 * 60  # secondi: il tempo di controllare l'importo e salvare
MAX_TEXT = 20_000  # non si analizza di più: un scontrino non è un libro
MAX_AMOUNT = Decimal("999999.99")


@dataclass(frozen=True)
class Reading:
    amount: Decimal
    line: str  # la riga dello scontrino da cui arriva l'importo, da mostrare a chi controlla


def available():
    """C'è Tesseract (e la lettura non è stata spenta con OCR_ENABLED=0)?"""
    command = settings.TESSERACT_CMD
    return bool(settings.OCR_ENABLED and command and shutil.which(command))


@lru_cache(maxsize=4)
def _language(command):
    """Italiano e inglese se ci sono entrambi, altrimenti inglese: per le cifre e «TOTALE» basta."""
    try:
        done = subprocess.run(
            [command, "--list-langs"], capture_output=True, text=True, timeout=10, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return "eng"
    langs = set(done.stdout.split())
    return "ita+eng" if {"ita", "eng"} <= langs else "eng"


def recognize(image_bytes):
    """Il testo letto da Tesseract, oppure None se non è disponibile o non ci riesce."""
    if not available():
        return None
    command = settings.TESSERACT_CMD
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            # Scala di grigi e contrasto: aiutano con la carta termica sbiadita.
            prepared = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
        with tempfile.TemporaryDirectory(prefix="scontrino-") as folder:
            path = Path(folder) / "scontrino.png"
            prepared.save(path, "PNG")
            done = subprocess.run(
                [command, str(path), "stdout", "-l", _language(command), "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                # Un solo thread: sul server condiviso non deve prendersi tutta la CPU.
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
    except (OSError, ValueError, subprocess.SubprocessError, Image.DecompressionBombError):
        return None
    return done.stdout[:MAX_TEXT] if done.returncode == 0 else None


# --- Dal testo all'importo ---------------------------------------------------------

# 947,50 · 1.234,00 · 1 234,00 · 947.50 · 947 ,50 (Tesseract a volte stacca la virgola)
AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[. ]\d{3})+|\d+)\s?[,.]\s?(\d{2})(?!\d)")

# Righe che dicono il totale della giornata o del documento: la prima che c'è vale.
STRONG = re.compile(
    r"total[ei]\s+(?:complessiv|giornalier|generale|incasso|corrispettiv|contabil|euro|eur\b)"
    r"|incasso\s+(?:giornalier|total)"
    r"|corrispettiv\w*\s+(?:giornalier|total)"
)
# Altre righe con «totale»: valgono solo se non sono una parte del totale (contanti, IVA…).
GENERIC = re.compile(r"\btotal[ei]\b")
PART_OF_TOTAL = re.compile(
    r"\biva\b|imponibil|imposta|sconto|resto|contant|elettronic|\bcart[ae]\b|bancomat|non riscoss|"
    r"pagat|buon[io]|ticket|credito|repart"
)
# Righe che non sono il totale dell'incasso nemmeno se dicono «totale complessivo».
NEVER = re.compile(r"annull|\bres[oi]\b|gran total|progressiv|cumulat|differenz")
ONLY_AMOUNT = re.compile(r"^\W*(?:eur\w*|€)?\W*\d[\d.,\s]*\d\W*(?:eur\w*|€)?\W*$")


def _plain(text):
    """Minuscolo e senza accenti: «Totale» e «TOTALE», «giornaliero» e «giornalièro» si confrontano uguali."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _tidy(line):
    """La riga da mostrare: spazi ripuliti e «947 ,50» (come lo legge Tesseract) di nuovo «947,50»."""
    line = " ".join(line.split())
    line = re.sub(r"(\d) +([,.]) ?(\d{2})(?!\d)", r"\1\2\3", line)
    return re.sub(r"(\d)([,.]) +(\d{2})(?!\d)", r"\1\2\3", line)[:80]


def _amounts(line):
    found = []
    for whole, cents in AMOUNT.findall(line):
        value = Decimal(f"{re.sub(r'[. ]', '', whole)}.{cents}")
        if Decimal("0") < value <= MAX_AMOUNT:
            found.append(value)
    return found


def find_total(text):
    """Il totale che c'è nel testo di uno scontrino come `Reading`, oppure None se non si capisce.

    Vale l'importo della prima riga «Totale complessivo / giornaliero / euro…» (l'ultimo importo
    della riga, o la riga sotto se questa ha solo la scritta). In mancanza, il più alto tra le altre
    righe «Totale» che non siano una parte del totale. Non si indovina mai da importi senza etichetta.
    """
    if not text:
        return None
    lines = [line.strip() for line in text[:MAX_TEXT].splitlines() if line.strip()]
    strong, generic = [], []
    for index, line in enumerate(lines):
        plain = _plain(line)
        if NEVER.search(plain):
            continue
        if STRONG.search(plain):
            kind = strong
        elif GENERIC.search(plain) and not PART_OF_TOTAL.search(plain):
            kind = generic
        else:
            continue
        amounts = _amounts(line)
        if not amounts and index + 1 < len(lines) and ONLY_AMOUNT.match(_plain(lines[index + 1])):
            amounts = _amounts(lines[index + 1])
        if amounts:
            kind.append((amounts[-1], line))
    if strong:
        amount, line = strong[0]
    elif generic:
        amount, line = max(generic, key=lambda item: item[0])
    else:
        return None
    return Reading(amount=amount, line=_tidy(line))


def read_total(image_bytes):
    """Legge la foto e ne ricava il totale: `Reading` oppure None."""
    return find_total(recognize(image_bytes))


# --- Lettura già fatta, da non ripetere ----------------------------------------------
#
# Quando l'importo si legge mentre si sceglie la foto, al salvataggio non serve rileggerla (costa CPU
# sul server). Chi legge rilascia un gettone firmato che lega l'importo letto a quella foto e a
# quell'utente: se al salvataggio la foto è la stessa, ci si fida; altrimenti si rilegge. Il gettone
# non si può falsificare né riusare con un'altra foto o da un altro utente.


def _digest(image_bytes):
    return hashlib.sha256(image_bytes).hexdigest()


def sign_reading(user_id, image_bytes, reading):
    payload = {
        "u": user_id,
        "h": _digest(image_bytes),
        "a": None if reading is None else str(reading.amount),
        "l": "" if reading is None else reading.line,
    }
    return signing.dumps(payload, salt=TOKEN_SALT, compress=True)


def verify_reading(token, user_id, image_bytes):
    """(True, Reading o None) se il gettone è valido per questa foto e questo utente, altrimenti (False, None)."""
    try:
        payload = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        if payload["u"] != user_id or payload["h"] != _digest(image_bytes):
            return False, None
        if payload["a"] is None:
            return True, None
        return True, Reading(amount=Decimal(payload["a"]), line=str(payload["l"])[:80])
    except (signing.BadSignature, KeyError, TypeError, ArithmeticError, ValueError):
        return False, None
