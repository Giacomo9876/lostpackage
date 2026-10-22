"""Lettura del totale da una foto di scontrino: il testo di Tesseract diventa un importo, senza mai indovinare."""
import os
import subprocess
import unittest
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core import signing
from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageDraw, ImageFont

from .. import ocr

D = Decimal

Z_REPORT = """\
I PACCHI PERDUTI SRL
MILANOFIORI ASSAGO
DOCUMENTO GESTIONALE
CHIUSURA GIORNALIERA
Data 20-10-2026 Ora 21:34
Scontrini emessi 47
Totale contanti 612,50
Totale elettronico 335,00
Totale non riscosso 0,00
TOTALE COMPLESSIVO 947,50
di cui IVA 22% 170,86
Imponibile 776,64
"""


class FindTotalTests(SimpleTestCase):
    def total(self, text):
        reading = ocr.find_total(text)
        return None if reading is None else reading.amount

    def test_a_daily_closing_report(self):
        reading = ocr.find_total(Z_REPORT)
        self.assertEqual(reading.amount, D("947.50"))
        self.assertEqual(reading.line, "TOTALE COMPLESSIVO 947,50")

    def test_the_closing_report_of_a_real_register(self):
        # Trascritto da una foto vera (Tor Vergata): il totale è «Totale Contabile», lo sbilancio no.
        text = (
            "Totale CHIUS.OPERATORE 0,00\nDettaglio Contabile\n"
            "CONTANTI 438,66\nCARTA 2193,60\nVALUTE 0,00\nTICKET 0,00\nNON RISCOSSO 0,00\n"
            "ALTRI PAG. ELETTRONICI 0,00\nBONIFICO 0,00\nASSEGNO 0,00\n"
            "Totale Contabile 2632 ,26\nSbilancio (+/-) -2632,26\n"
        )
        reading = ocr.find_total(text)
        self.assertEqual((reading.amount, reading.line), (D("2632.26"), "Totale Contabile 2632,26"))

    def test_the_accounting_total_wins_over_an_operator_total(self):
        text = "Totale CHIUS.OPERATORE 5000,00\nTotale Contabile 2632,26\n"
        self.assertEqual(self.total(text), D("2632.26"))

    def test_the_parts_of_the_total_are_never_taken_for_it(self):
        text = "Totale contanti 612,50\nTotale elettronico 335,00\nIVA 22% 170,86\nImponibile 776,64\n"
        self.assertIsNone(self.total(text))

    def test_other_wordings_of_the_daily_total(self):
        cases = {
            "CORRISPETTIVO GIORNALIERO\nContanti 1.234,00\nCarte 280,50\nTOTALE EURO 1.514,50\nIVA 22% 273,10": "1514.50",
            "TOTALE GIORNALIERO 380,00": "380.00",
            "Totale incasso 99,90": "99.90",
            "INCASSO GIORNALIERO 1.050,00": "1050.00",
            "TOTALE CORRISPETTIVI 210,00": "210.00",
            "TOTALE GENERALE: 75,00": "75.00",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.total(text), D(expected))

    def test_amount_formats(self):
        cases = {
            "TOTALE COMPLESSIVO 947,50": "947.50",
            "TOTALE COMPLESSIVO 947 ,50": "947.50",  # come lo scrive Tesseract a volte
            "TOTALE COMPLESSIVO 947, 50": "947.50",
            "TOTALE COMPLESSIVO 947.50": "947.50",
            "TOTALE COMPLESSIVO 1.947,50": "1947.50",
            "TOTALE COMPLESSIVO 1 947,50": "1947.50",
            "TOTALE COMPLESSIVO 1947,50": "1947.50",
            "TOTALE COMPLESSIVO EUR 947,50": "947.50",
            "TOTALE COMPLESSIVO € 947,50": "947.50",
            "TOTALE COMPLESSIVO 947,50 EUR": "947.50",
            "TOTALE COMPLESSIVO 0,50": "0.50",
            "TOTALE COMPLESSIVO 12.345,67": "12345.67",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.total(text), D(expected))

    def test_the_last_amount_on_the_line_is_the_total(self):
        self.assertEqual(self.total("TOTALE COMPLESSIVO 12 articoli 31,25"), D("31.25"))
        self.assertEqual(self.total("TOTALE COMPLESSIVO 2,00 31,25"), D("31.25"))

    def test_the_amount_on_the_next_line(self):
        self.assertEqual(self.total("TOTALE COMPLESSIVO\n947,50\nIVA 22% 170,86"), D("947.50"))
        self.assertEqual(self.total("TOTALE COMPLESSIVO\n€ 947,50"), D("947.50"))
        # …ma solo se la riga sotto è un importo e basta
        self.assertIsNone(self.total("TOTALE COMPLESSIVO\nGrazie e arrivederci 12,00"))

    def test_a_normal_receipt(self):
        text = "PACCO 1,250 KG 31,25\nTOTALE COMPLESSIVO 31,25\nPagamento contanti 50,00\nResto 18,75\n"
        self.assertEqual(self.total(text), D("31.25"))

    def test_the_first_total_line_wins(self):
        text = "TOTALE COMPLESSIVO 100,00\nTOTALE COMPLESSIVO 250,00\n"
        self.assertEqual(self.total(text), D("100.00"))

    def test_cancellations_cumulative_and_refunds_are_ignored(self):
        text = (
            "TOTALE COMPLESSIVO ANNULLATI 12,00\nTOTALE COMPLESSIVO RESI 5,00\n"
            "GRAN TOTALE 123456,78\nTOTALE PROGRESSIVO 5000,00\nTOTALE COMPLESSIVO 947,50\n"
        )
        self.assertEqual(self.total(text), D("947.50"))

    def test_without_an_exact_wording_the_largest_plain_total_is_used(self):
        self.assertEqual(self.total("TOTALE 12,00\nTOTALE 31,25\n"), D("31.25"))
        self.assertEqual(self.total("Totale 31,25"), D("31.25"))

    def test_upper_lower_case_and_accents(self):
        self.assertEqual(self.total("totale complessivo 947,50"), D("947.50"))
        self.assertEqual(self.total("Totale Giornalièro 380,00"), D("380.00"))

    def test_never_guesses_from_unlabelled_amounts(self):
        for text in ("12,00\n31,25\n947,50", "Grazie per l'acquisto\nBuona giornata", "", None, "TOTALE", "TOTALE COMPLESSIVO"):
            with self.subTest(text=text):
                self.assertIsNone(self.total(text))

    def test_junk_amounts_are_not_amounts(self):
        for text in (
            "TOTALE COMPLESSIVO 0,00",  # zero: più probabilmente rumore che una giornata vuota
            "TOTALE COMPLESSIVO 947",  # senza centesimi
            "TOTALE COMPLESSIVO 94,5",
            "TOTALE COMPLESSIVO 1.234.567,89",  # oltre il massimo
            "TOTALE COMPLESSIVO 12-34",
            "TOTALE COMPLESSIVO 20-10-2026",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.total(text))

    def test_a_huge_text_does_not_hang(self):
        text = ("x " * 50_000) + "\nTOTALE COMPLESSIVO 12,00"
        self.assertIsNone(self.total(text))  # oltre il limite di lettura: ignorato, senza errori

    def test_the_line_shown_is_tidied_up(self):
        self.assertEqual(ocr.find_total("TOTALE   COMPLESSIVO 947 ,50").line, "TOTALE COMPLESSIVO 947,50")
        self.assertEqual(ocr.find_total("TOTALE EURO 1 947, 50").line, "TOTALE EURO 1 947,50")
        self.assertEqual(ocr.find_total("TOTALE EURO 1.514,50").line, "TOTALE EURO 1.514,50")

    def test_the_line_shown_is_trimmed(self):
        reading = ocr.find_total("TOTALE   COMPLESSIVO " + "." * 200 + " 947,50")
        self.assertLessEqual(len(reading.line), 80)


class AvailabilityTests(SimpleTestCase):
    def test_without_tesseract_nothing_is_read_and_nothing_breaks(self):
        with override_settings(TESSERACT_CMD="/non/esiste/tesseract"):
            self.assertFalse(ocr.available())
            self.assertIsNone(ocr.recognize(b"qualsiasi cosa"))
            self.assertIsNone(ocr.read_total(b"qualsiasi cosa"))

    def test_it_can_be_switched_off(self):
        with override_settings(OCR_ENABLED=False, TESSERACT_CMD="tesseract"):
            self.assertFalse(ocr.available())

    def test_an_empty_command_means_off(self):
        with override_settings(TESSERACT_CMD=""):
            self.assertFalse(ocr.available())

    def test_a_file_that_is_not_an_image_gives_none(self):
        with mock.patch.object(ocr, "available", return_value=True):
            self.assertIsNone(ocr.recognize(b"%PDF-1.4 non sono una foto"))

    def test_a_timeout_gives_none(self):
        image = BytesIO()
        Image.new("RGB", (50, 50), "white").save(image, "JPEG")
        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(
            ocr.subprocess, "run", side_effect=subprocess.TimeoutExpired("tesseract", 25)
        ):
            self.assertIsNone(ocr.recognize(image.getvalue()))

    def test_a_failing_tesseract_gives_none(self):
        image = BytesIO()
        Image.new("RGB", (50, 50), "white").save(image, "JPEG")
        failed = subprocess.CompletedProcess([], 1, stdout="TOTALE COMPLESSIVO 12,00", stderr="boom")
        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(
            ocr.subprocess, "run", return_value=failed
        ):
            self.assertIsNone(ocr.recognize(image.getvalue()))

    def test_tesseract_is_called_without_a_shell_and_with_a_time_limit(self):
        image = BytesIO()
        Image.new("RGB", (50, 50), "white").save(image, "JPEG")
        ok = subprocess.CompletedProcess([], 0, stdout="TOTALE COMPLESSIVO 12,00", stderr="")
        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(
            ocr, "_language", return_value="eng"
        ), mock.patch.object(ocr.subprocess, "run", return_value=ok) as run:
            self.assertEqual(ocr.read_total(image.getvalue()).amount, D("12.00"))
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)  # niente stringa di shell
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], ocr.TIMEOUT_SECONDS)
        self.assertEqual(kwargs["env"]["OMP_THREAD_LIMIT"], "1")
        self.assertEqual(args[0][0], "tesseract")
        self.assertEqual(args[0][2:], ["stdout", "-l", "eng", "--psm", "6"])


class ReadingTokenTests(SimpleTestCase):
    """Il gettone firmato lega l'importo letto a una foto e a un utente: non si falsifica né si riusa."""

    IMAGE = b"i byte della foto ricodificata"
    READING = ocr.Reading(D("947.50"), "TOTALE COMPLESSIVO 947,50")

    def test_a_token_gives_back_the_reading_for_the_same_photo_and_user(self):
        token = ocr.sign_reading(7, self.IMAGE, self.READING)
        self.assertEqual(ocr.verify_reading(token, 7, self.IMAGE), (True, self.READING))

    def test_a_reading_of_nothing_is_remembered_too(self):
        token = ocr.sign_reading(7, self.IMAGE, None)
        self.assertEqual(ocr.verify_reading(token, 7, self.IMAGE), (True, None))

    def test_another_photo_or_another_user_is_refused(self):
        token = ocr.sign_reading(7, self.IMAGE, self.READING)
        self.assertEqual(ocr.verify_reading(token, 7, self.IMAGE + b"x"), (False, None))
        self.assertEqual(ocr.verify_reading(token, 8, self.IMAGE), (False, None))

    def test_a_modified_or_foreign_token_is_refused(self):
        token = ocr.sign_reading(7, self.IMAGE, self.READING)
        for bad in (token[:-2] + "zz", "x" + token, "", "abc", token.split(":")[0], None, 12):
            with self.subTest(bad=bad):
                self.assertEqual(ocr.verify_reading(bad, 7, self.IMAGE), (False, None))

    def test_a_token_signed_for_something_else_is_refused(self):
        other = signing.dumps({"u": 7, "h": ocr._digest(self.IMAGE), "a": "1.00", "l": ""}, salt="altro.uso")
        self.assertEqual(ocr.verify_reading(other, 7, self.IMAGE), (False, None))

    def test_a_token_expires(self):
        token = ocr.sign_reading(7, self.IMAGE, self.READING)
        with mock.patch.object(ocr, "TOKEN_MAX_AGE", -1):
            self.assertEqual(ocr.verify_reading(token, 7, self.IMAGE), (False, None))

    def test_a_signed_but_malformed_payload_is_refused(self):
        for payload in ({"u": 7}, {"u": 7, "h": ocr._digest(self.IMAGE), "a": "non un numero", "l": ""}, [1, 2], "testo"):
            with self.subTest(payload=payload):
                token = signing.dumps(payload, salt=ocr.TOKEN_SALT)
                self.assertEqual(ocr.verify_reading(token, 7, self.IMAGE), (False, None))

    def test_the_line_in_a_token_is_trimmed(self):
        token = ocr.sign_reading(7, self.IMAGE, ocr.Reading(D("1.00"), "x" * 500))
        self.assertLessEqual(len(ocr.verify_reading(token, 7, self.IMAGE)[1].line), 80)


def _receipt_image(lines, size=30, angle=0):
    font_path = next(
        (p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf") if os.path.exists(p)),
        None,
    )
    if font_path is None:
        return None
    font = ImageFont.truetype(font_path, size)
    step = int(size * 1.4)
    img = Image.new("RGB", (760, 40 + step * len(lines)), (246, 244, 238))
    draw = ImageDraw.Draw(img)
    for number, line in enumerate(lines):
        draw.text((24, 20 + step * number), line, font=font, fill=(35, 35, 38))
    if angle:
        img = img.rotate(angle, expand=True, fillcolor=(70, 62, 55))
    out = BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


@unittest.skipUnless(ocr.available(), "Tesseract non è installato su questo computer")
class RealTesseractTests(SimpleTestCase):
    """Con Tesseract vero: un'immagine di scontrino disegnata qui, anche storta."""

    LINES = [
        "  I PACCHI PERDUTI SRL",
        "  CHIUSURA GIORNALIERA",
        "Data 20-10-2026   Ora 21:34",
        "Scontrini emessi              47",
        "Totale contanti           612,50",
        "Totale elettronico        335,00",
        "TOTALE COMPLESSIVO        947,50",
        "di cui IVA 22%            170,86",
    ]

    def test_it_reads_the_total_of_a_closing_report(self):
        data = _receipt_image(self.LINES)
        if data is None:
            self.skipTest("nessun carattere a spaziatura fissa per disegnare lo scontrino")
        reading = ocr.read_total(data)
        self.assertIsNotNone(reading)
        self.assertEqual(reading.amount, D("947.50"))

    def test_it_reads_a_slightly_crooked_photo(self):
        data = _receipt_image(self.LINES, angle=3)
        if data is None:
            self.skipTest("nessun carattere a spaziatura fissa per disegnare lo scontrino")
        self.assertEqual(ocr.read_total(data).amount, D("947.50"))

    def test_a_picture_without_text_gives_nothing(self):
        out = BytesIO()
        Image.new("RGB", (400, 300), "white").save(out, "JPEG")
        self.assertIsNone(ocr.read_total(out.getvalue()))
