"""Elaborazione delle foto caricate dal telefono.

Una foto da smartphone pesa 4-10 MB, ha l'orientamento salvato nei metadati
EXIF e spesso contiene le coordinate GPS. Qui la foto viene ricodificata:
raddrizzata, ridimensionata, convertita in JPEG e privata dei metadati.
Ricodificare serve anche alla sicurezza: ciò che finisce su disco è sempre
un JPEG generato da noi, mai il file originale caricato.
"""
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

PHOTO_MAX_SIDE = 1600
THUMB_MAX_SIDE = 480
RECEIPT_MAX_SIDE = 2400  # uno scontrino è lungo e stretto: serve più risoluzione per leggere i numeri


def _to_jpeg(img, max_side, quality):
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    copy.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return ContentFile(buffer.getvalue())


def _open_rgb(uploaded, max_side):
    """Apre una foto caricata, la raddrizza (EXIF) e la porta in RGB; ValidationError se non è una foto."""
    try:
        with Image.open(uploaded) as img:
            img.draft("RGB", (max_side, max_side))  # decodifica ridotta per i JPEG grandi
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.getchannel("A"))
                return flat
            return img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ValidationError("Il file non è una foto leggibile. Scatta di nuovo o scegli una foto JPEG o PNG.")


def process_upload(uploaded):
    """Ritorna (foto, miniatura) come ContentFile JPEG, oppure solleva ValidationError."""
    img = _open_rgb(uploaded, PHOTO_MAX_SIDE)
    return _to_jpeg(img, PHOTO_MAX_SIDE, 82), _to_jpeg(img, THUMB_MAX_SIDE, 75)


def process_receipt(uploaded):
    """Foto di uno scontrino: un solo JPEG, più grande delle foto dei pacchi perché il testo si legga.

    Ritorna un ContentFile JPEG, oppure solleva ValidationError.
    """
    return _to_jpeg(_open_rgb(uploaded, RECEIPT_MAX_SIDE), RECEIPT_MAX_SIDE, 85)
