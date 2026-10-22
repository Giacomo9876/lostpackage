"""Sezione Scontrini: modulo di chiusura (importo, foto, Yakuza), lettura dalla foto, elenco e medie."""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .. import averages, ocr
from ..images import process_receipt
from ..models import DayClose
from .helpers import BaseCase, make_image, make_user, store

D = Decimal
TODAY = date(2026, 10, 21)  # mercoledì
YESTERDAY = TODAY - timedelta(days=1)


def YESTERDAY_REAL():  # noqa: N802 - ieri rispetto all'orologio vero (i test senza data fissa)
    return timezone.localdate() - timedelta(days=1)
MON_5, TUE_6, SAT_10, SUN_11 = (date(2026, 10, d) for d in (5, 6, 10, 11))

READING = ocr.Reading(D("947.50"), "TOTALE COMPLESSIVO 947,50")

CLOSE = reverse("inventory:close_day")
RECEIPTS = reverse("inventory:receipts")
READ = reverse("inventory:receipt_read")


def upload(size=(600, 900), name="scontrino.jpg"):
    return make_image(size, name=name)


def processed_bytes(file):
    """La foto come la tiene l'app dopo la ricodifica: è su questa che si firma la lettura."""
    file.seek(0)
    data = process_receipt(file).read()
    file.seek(0)
    return data


def make_close(day, receipts, yakuza="0", store_name="Milanofiori", **extra):
    return DayClose.objects.create(
        store=store(store_name), date=day, receipts_total=D(str(receipts)), yakuza_cash=D(str(yakuza)), **extra
    )


def make_close_with_photo(day=None, receipts="100", store_name="Milanofiori", **extra):
    close = make_close(day or timezone.localdate(), receipts, store_name=store_name, **extra)
    buffer = BytesIO()
    Image.new("RGB", (80, 120), "navy").save(buffer, "JPEG")
    close.photo.save("x.jpg", ContentFile(buffer.getvalue()), save=True)
    return close


@override_settings(OCR_ENABLED=False)
class ReceiptCase(BaseCase):
    """Di base la lettura è spenta: l'importo si scrive. Con `fake_ocr()` si accende, con Tesseract finto."""

    def fake_ocr(self, reading=READING):
        available = mock.patch.object(ocr, "available", return_value=True)
        read = mock.patch.object(ocr, "read_total", return_value=reading)
        available.start()
        self.addCleanup(available.stop)
        mock_read = read.start()
        self.addCleanup(read.stop)
        return mock_read

    def freeze_today(self, day=TODAY):
        real = timezone.localdate
        patcher = mock.patch.object(timezone, "localdate", side_effect=lambda value=None: day if value is None else real(value))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, data=None, url=CLOSE, **kwargs):
        return self.client.post(url, data or {}, **kwargs)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]


# --- Il modulo ------------------------------------------------------------------------


class FormPageTests(ReceiptCase):
    def test_page_opens_with_amount_yakuza_and_photo_fields(self):
        response = self.client.get(CLOSE)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "inventory/close_day.html")
        self.assertEqual(response.context["store"], store("Milanofiori"))
        self.assertEqual(response.context["day"], timezone.localdate())
        self.assertContains(response, 'name="receipts_total"')
        self.assertContains(response, 'name="yakuza_cash"')
        self.assertContains(response, 'accept="image/*"')
        self.assertContains(response, 'capture="environment"')
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, "Contanti Yakuza")

    def test_without_reading_the_form_says_to_type_the_amount(self):
        response = self.client.get(CLOSE)
        self.assertFalse(response.context["ocr_available"])
        self.assertNotContains(response, "data-read-url")
        self.assertContains(response, "Scrivi tu l'importo")

    def test_with_reading_the_form_knows_the_endpoint(self):
        self.fake_ocr()
        response = self.client.get(CLOSE)
        self.assertTrue(response.context["ocr_available"])
        self.assertContains(response, f'data-read-url="{READ}"')
        self.assertContains(response, "Fotografa lo scontrino di chiusura")

    def test_an_existing_close_fills_the_form(self):
        make_close(timezone.localdate(), "123.40", "20.00")
        response = self.client.get(CLOSE)
        self.assertContains(response, 'value="123,40"')
        self.assertContains(response, 'value="20,00"')
        self.assertContains(response, "Salva le modifiche")
        self.assertContains(response, reverse("inventory:close_delete", args=[DayClose.objects.get().pk]))

    def test_a_new_day_has_no_delete_button(self):
        self.assertNotContains(self.client.get(CLOSE), "Elimina questa chiusura")

    def test_the_photo_of_a_close_is_shown_in_the_form(self):
        close = make_close_with_photo()
        response = self.client.get(CLOSE)
        self.assertContains(response, reverse("inventory:receipt_photo", args=[close.pk]))
        self.assertContains(response, "Togli la foto")

    def test_a_manager_has_today_and_yesterday_and_no_date_picker(self):
        response = self.client.get(CLOSE)
        self.assertContains(response, f"?data={(timezone.localdate() - timedelta(days=1)).isoformat()}")
        self.assertNotContains(response, 'type="date"')

    def test_an_owner_must_choose_a_center_first(self):
        self.as_owner()
        response = self.client.get(CLOSE)
        self.assertTemplateUsed(response, "inventory/choose_store.html")
        self.assertEqual(response.context["title"], "Scontrini: scegli il centro")
        response = self.client.get(CLOSE, {"centro": store("Globo").pk})
        self.assertTemplateUsed(response, "inventory/close_day.html")
        self.assertContains(response, 'type="date"')
        self.assertContains(response, f'max="{timezone.localdate().isoformat()}"')

    def test_the_form_needs_a_login_and_a_role(self):
        self.client.logout()
        self.assertEqual(self.client.get(CLOSE).status_code, 302)
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.get(CLOSE).status_code, 403)
        self.assertEqual(self.client.post(CLOSE, {"receipts_total": "10"}).status_code, 403)
        self.assertFalse(DayClose.objects.exists())


class SaveTests(ReceiptCase):
    def test_amount_and_yakuza_are_saved_for_today(self):
        response = self.save({"receipts_total": "100,50", "yakuza_cash": "20"})
        self.assertRedirects(response, RECEIPTS, fetch_redirect_response=False)
        close = DayClose.objects.get()
        self.assertEqual((close.store, close.date), (store("Milanofiori"), timezone.localdate()))
        self.assertEqual((close.receipts_total, close.yakuza_cash), (D("100.50"), D("20.00")))
        self.assertEqual(close.closed_by, self.user)
        self.assertFalse(close.has_photo)
        self.assertIsNone(close.detected_total)
        self.assertEqual((close.day_total, close.reading), (D("120.50"), "typed"))

    def test_yakuza_is_optional(self):
        self.save({"receipts_total": "50"})
        self.assertEqual(DayClose.objects.get().yakuza_cash, D("0.00"))

    def test_the_confirmation_message_mentions_yakuza_only_when_there_is_some(self):
        response = self.save({"receipts_total": "50", "yakuza_cash": "12,5"}, follow=True)
        self.assertIn("scontrini 50,00 € e contanti Yakuza 12,50 €", self.messages(response)[0])
        DayClose.objects.all().delete()
        response = self.save({"receipts_total": "50"}, follow=True)
        self.assertNotIn("Yakuza", self.messages(response)[0])

    def test_amount_is_required_without_a_photo(self):
        response = self.save({"receipts_total": "", "yakuza_cash": "10"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["receipts_total"], ["Scrivi il totale degli scontrini."])
        self.assertContains(response, "Scrivi il totale degli scontrini.")
        self.assertFalse(DayClose.objects.exists())

    def test_invalid_amounts_and_yakuza_are_rejected(self):
        cases = [
            ({"receipts_total": "-1"}, "receipts_total"),
            ({"receipts_total": "abc"}, "receipts_total"),
            ({"receipts_total": "10000000"}, "receipts_total"),
            ({"receipts_total": "10", "yakuza_cash": "-5"}, "yakuza_cash"),
            ({"receipts_total": "10", "yakuza_cash": "abc"}, "yakuza_cash"),
            ({"receipts_total": "10", "yakuza_cash": "10000000"}, "yakuza_cash"),
        ]
        for data, field in cases:
            with self.subTest(data=data):
                response = self.save(data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
        self.assertFalse(DayClose.objects.exists())

    def test_saving_again_updates_the_same_close(self):
        self.save({"receipts_total": "100", "yakuza_cash": "10"})
        self.save({"receipts_total": "90,50", "yakuza_cash": ""})
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.yakuza_cash), (D("90.50"), D("0.00")))

    def test_zero_is_a_valid_amount(self):
        self.assertEqual(self.save({"receipts_total": "0"}).status_code, 302)
        self.assertEqual(DayClose.objects.get().receipts_total, D("0.00"))

    def test_a_manager_works_on_yesterday_but_not_on_older_days(self):
        self.save({"receipts_total": "10"}, url=f"{CLOSE}?data={YESTERDAY_REAL().isoformat()}")
        old = timezone.localdate() - timedelta(days=3)
        response = self.save({"receipts_total": "20"}, url=f"{CLOSE}?data={old.isoformat()}", follow=True)
        # un giorno che non si può toccare non diventa «oggi» in silenzio: non si salva nulla
        self.assertTrue(any("non ho salvato nulla" in text for text in self.messages(response)))
        self.assertEqual(list(DayClose.objects.values_list("date", "receipts_total")), [(YESTERDAY_REAL(), D("10.00"))])

    def test_an_invalid_or_future_day_in_the_address_saves_nothing(self):
        future = timezone.localdate() + timedelta(days=1)
        for raw in ("abc", "2026-13-45", future.isoformat()):
            with self.subTest(raw=raw):
                response = self.save({"receipts_total": "20"}, url=f"{CLOSE}?data={raw}")
                self.assertRedirects(response, RECEIPTS, fetch_redirect_response=False)
        self.assertFalse(DayClose.objects.exists())

    def test_a_manager_cannot_choose_the_day_in_the_form(self):
        old = timezone.localdate() - timedelta(days=30)
        self.save({"receipts_total": "20", "day": old.isoformat()})
        self.assertEqual(DayClose.objects.get().date, timezone.localdate())

    def test_an_owner_fills_any_past_day_of_the_chosen_center(self):
        self.as_owner()
        old = timezone.localdate() - timedelta(days=30)
        response = self.save(
            {"receipts_total": "77", "day": old.isoformat()}, url=f"{CLOSE}?centro={store('Globo').pk}&nuova=1"
        )
        self.assertRedirects(response, f"{RECEIPTS}?centro={store('Globo').pk}", fetch_redirect_response=False)
        close = DayClose.objects.get()
        self.assertEqual((close.store, close.date, close.closed_by), (store("Globo"), old, self.owner))

    def test_the_center_comes_from_the_address_never_from_the_form(self):
        self.save({"receipts_total": "10", "store": store("Globo").pk})
        self.assertEqual(DayClose.objects.get().store, store("Milanofiori"))

    def test_the_form_needs_post(self):
        self.assertEqual(self.client.put(CLOSE, {"receipts_total": "1"}).status_code, 405)


class OwnerChoosesTheDayTests(ReceiptCase):
    """Il titolare aggiunge giornate passate: il giorno scelto nel modulo è quello salvato, senza sovrascrivere."""

    def setUp(self):
        super().setUp()
        self.as_owner()
        self.globo = store("Globo").pk
        self.url = f"{CLOSE}?centro={self.globo}"
        self.add_url = f"{CLOSE}?centro={self.globo}&nuova=1"
        self.today = timezone.localdate()
        self.old = self.today - timedelta(days=12)

    def test_the_day_chosen_in_the_form_is_the_day_saved(self):
        # Il caso dello scontrino del 10 ottobre finito su «oggi»: la pagina era aperta su oggi.
        response = self.client.get(self.url)
        self.assertTrue(response.context["adding"])
        self.assertContains(response, f'value="{self.today.isoformat()}"')
        self.save({"day": self.old.isoformat(), "receipts_total": "2632,26"}, url=self.url)
        close = DayClose.objects.get()
        self.assertEqual((close.date, close.receipts_total), (self.old, D("2632.26")))
        self.assertFalse(DayClose.objects.filter(date=self.today).exists())

    def test_the_date_field_is_inside_the_form_that_is_saved(self):
        page = self.client.get(self.url).content.decode()
        form = page[page.index('<form method="post" enctype="multipart/form-data"'):]
        form = form[: form.index("</form>")]
        self.assertIn('name="day"', form)
        self.assertNotIn('name="data"', page)  # niente più selettore separato con «Apri»

    def test_a_day_already_closed_is_never_overwritten(self):
        existing = make_close(self.old, "500", "40", store_name="Globo")
        response = self.save({"day": self.old.isoformat(), "receipts_total": "2632,26", "photo": upload()}, url=self.add_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("già chiuso", response.context["form"].errors["day"][0])
        self.assertIn("non ho salvato nulla", response.context["form"].errors["day"][0])
        self.assertEqual(response.context["conflict_url"], f"{CLOSE}?data={self.old.isoformat()}&centro={self.globo}")
        self.assertContains(response, f"{CLOSE}?data={self.old.isoformat()}&amp;centro={self.globo}")
        existing.refresh_from_db()
        self.assertEqual((existing.receipts_total, existing.yakuza_cash, existing.has_photo), (D("500.00"), D("40.00"), False))
        self.assertEqual(DayClose.objects.count(), 1)

    def test_with_today_closed_the_page_edits_today_and_offers_to_add_another_day(self):
        make_close(self.today, "100", store_name="Globo")
        response = self.client.get(self.url)
        self.assertFalse(response.context["adding"])
        self.assertIsNotNone(response.context["close"])
        self.assertNotContains(response, 'name="day"')
        self.assertContains(response, "Aggiungi un'altra giornata")
        self.assertContains(response, "già chiusa")

    def test_adding_starts_blank_when_today_is_already_closed(self):
        make_close(self.today, "100", store_name="Globo")
        response = self.client.get(self.add_url)
        self.assertTrue(response.context["adding"])
        self.assertIsNone(response.context["close"])
        self.assertNotContains(response, "Elimina questa chiusura")
        self.assertIsNone(response.context["form"]["day"].value())
        response = self.save({"receipts_total": "10"}, url=self.add_url)
        self.assertEqual(response.context["form"].errors["day"], ["Scegli la giornata dello scontrino."])

    def test_future_and_far_away_days_are_refused(self):
        for raw, message in (
            ((self.today + timedelta(days=1)).isoformat(), "nel futuro"),
            ("2019-12-31", "troppo indietro"),
            ("0202-10-10", "troppo indietro"),
            ("10-10", "valida"),
        ):
            with self.subTest(raw=raw):
                response = self.save({"day": raw, "receipts_total": "10"}, url=self.add_url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, " ".join(response.context["form"].errors["day"]))
        self.assertFalse(DayClose.objects.exists())

    def test_the_italian_date_format_works_too(self):
        self.save({"day": self.old.strftime("%d/%m/%Y"), "receipts_total": "10"}, url=self.add_url)
        self.assertEqual(DayClose.objects.get().date, self.old)

    def test_editing_a_closed_day_keeps_its_day(self):
        close = make_close(self.old, "500", store_name="Globo")
        url = f"{CLOSE}?centro={self.globo}&data={self.old.isoformat()}"
        response = self.client.get(url)
        self.assertFalse(response.context["adding"])
        self.save({"receipts_total": "450", "day": self.today.isoformat()}, url=url)  # il giorno nel modulo non conta
        close.refresh_from_db()
        self.assertEqual((close.date, close.receipts_total), (self.old, D("450.00")))
        self.assertEqual(DayClose.objects.count(), 1)

    def test_the_message_names_the_day_saved(self):
        response = self.save({"day": self.old.isoformat(), "receipts_total": "10"}, url=self.url, follow=True)
        self.assertIn(f"Giornata del {self.old.day} ", self.messages(response)[0])
        self.assertIn(str(self.old.year), self.messages(response)[0])


class PhotoUploadTests(ReceiptCase):
    def test_a_photo_is_saved_as_a_private_reencoded_jpeg(self):
        response = self.save({"receipts_total": "100", "photo": upload(size=(3000, 4000), name="IMG_0001.png")})
        self.assertEqual(response.status_code, 302)
        close = DayClose.objects.get()
        self.assertTrue(close.has_photo)
        self.assertRegex(close.photo.name, r"^receipts/\d{4}/\d{2}/[0-9a-f]{32}\.jpg$")
        self.assertNotIn("IMG_0001", close.photo.name)
        with close.photo.open("rb") as handle:
            image = Image.open(handle)
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 2400)

    def test_without_reading_nothing_is_detected(self):
        self.save({"receipts_total": "100", "photo": upload()})
        self.assertIsNone(DayClose.objects.get().detected_total)

    def test_a_file_that_is_not_a_picture_is_refused(self):
        bogus = SimpleUploadedFile("scontrino.jpg", b"%PDF-1.4 non sono una foto", content_type="image/jpeg")
        response = self.save({"receipts_total": "100", "photo": bogus})
        self.assertEqual(response.status_code, 200)
        self.assertIn("photo", response.context["form"].errors)
        self.assertFalse(DayClose.objects.exists())

    @override_settings(MAX_UPLOAD_MB=0)
    def test_a_photo_over_the_limit_is_refused(self):
        response = self.save({"receipts_total": "100", "photo": upload()})
        self.assertEqual(response.status_code, 200)
        self.assertIn("La foto supera 0 MB", response.context["form"].errors["photo"][0])
        self.assertFalse(DayClose.objects.exists())

    def test_editing_the_amount_keeps_the_photo(self):
        close = make_close_with_photo(receipts="100", detected_total=D("100.00"))
        name = close.photo.name
        self.save({"receipts_total": "95"})
        close.refresh_from_db()
        self.assertEqual((close.photo.name, close.receipts_total, close.detected_total), (name, D("95.00"), D("100.00")))

    def test_a_new_photo_replaces_the_old_one_and_deletes_its_file(self):
        close = make_close_with_photo()
        old = close.photo.name
        self.assertTrue(close.photo.storage.exists(old))
        with self.captureOnCommitCallbacks(execute=True):
            self.save({"receipts_total": "100", "photo": upload()})
        close.refresh_from_db()
        self.assertNotEqual(close.photo.name, old)
        self.assertTrue(close.photo.storage.exists(close.photo.name))
        self.assertFalse(close.photo.storage.exists(old))

    def test_the_photo_can_be_removed(self):
        close = make_close_with_photo(detected_total=D("100.00"))
        old = close.photo.name
        with self.captureOnCommitCallbacks(execute=True):
            self.save({"receipts_total": "100", "remove_photo": "on"})
        close.refresh_from_db()
        self.assertFalse(close.has_photo)
        self.assertIsNone(close.detected_total)
        self.assertFalse(close.photo.storage.exists(old))

    def test_removing_a_photo_that_is_not_there_is_harmless(self):
        self.assertEqual(self.save({"receipts_total": "100", "remove_photo": "on"}).status_code, 302)
        self.assertFalse(DayClose.objects.get().has_photo)

    def test_a_refused_form_leaves_no_file_behind(self):
        before = set(self.media_files())
        self.save({"receipts_total": "-1", "photo": upload()})
        self.assertEqual(set(self.media_files()), before)

    def media_files(self):
        storage = DayClose._meta.get_field("photo").storage
        found = []
        if storage.exists("receipts"):
            for year in storage.listdir("receipts")[0]:
                for month in storage.listdir(f"receipts/{year}")[0]:
                    found += [f"{year}/{month}/{n}" for n in storage.listdir(f"receipts/{year}/{month}")[1]]
        return found


class ReadingTests(ReceiptCase):
    """L'importo letto dalla foto aiuta, ma chi chiude lo conferma o lo corregge."""

    def test_an_empty_amount_is_filled_from_the_photo(self):
        reader = self.fake_ocr()
        response = self.save({"receipts_total": "", "photo": upload()}, follow=True)
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total, close.reading), (D("947.50"), D("947.50"), "read"))
        self.assertIn("importo letto dalla foto, 947,50 €", self.messages(response)[0])
        reader.assert_called_once()

    def test_a_confirmed_amount_is_marked_as_read(self):
        self.fake_ocr()
        self.save({"receipts_total": "947,50", "photo": upload()})
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total, close.reading), (D("947.50"), D("947.50"), "read"))

    def test_a_corrected_amount_is_kept_and_the_reading_is_remembered(self):
        self.fake_ocr()
        response = self.save({"receipts_total": "940", "photo": upload()}, follow=True)
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total, close.reading), (D("940.00"), D("947.50"), "corrected"))
        self.assertNotIn("letto dalla foto", self.messages(response)[0])

    def test_an_unreadable_photo_asks_for_the_amount(self):
        self.fake_ocr(reading=None)
        response = self.save({"receipts_total": "", "photo": upload()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["receipts_total"], ["Non riesco a leggere l'importo dalla foto: scrivilo a mano."]
        )
        self.assertFalse(DayClose.objects.exists())

    def test_an_unreadable_photo_with_a_typed_amount_is_saved_as_typed(self):
        self.fake_ocr(reading=None)
        self.save({"receipts_total": "300", "photo": upload()})
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total, close.reading, close.has_photo), (D("300.00"), None, "typed", True))

    def test_without_a_photo_there_is_nothing_to_read(self):
        reader = self.fake_ocr()
        self.save({"receipts_total": "100"})
        reader.assert_not_called()
        self.assertEqual(DayClose.objects.get().reading, "typed")

    def test_a_reading_already_done_in_the_browser_is_not_repeated(self):
        reader = self.fake_ocr()
        data = self.client.post(READ, {"photo": upload()}).json()
        reader.assert_called_once()
        reader.reset_mock()
        self.save({"receipts_total": "", "photo": upload(), "reading_token": data["token"]})
        reader.assert_not_called()
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total), (D("947.50"), D("947.50")))

    def test_a_token_of_another_photo_is_not_trusted(self):
        reader = self.fake_ocr()
        data = self.client.post(READ, {"photo": upload(size=(500, 700))}).json()
        reader.reset_mock()
        self.save({"receipts_total": "", "photo": upload(size=(600, 900)), "reading_token": data["token"]})
        reader.assert_called_once()

    def test_a_token_of_another_user_is_not_trusted(self):
        reader = self.fake_ocr()
        data = self.client.post(READ, {"photo": upload()}).json()
        reader.reset_mock()
        self.as_owner()
        self.save(
            {"receipts_total": "", "photo": upload(), "reading_token": data["token"], "day": timezone.localdate().isoformat()},
            url=f"{CLOSE}?centro={store('Globo').pk}",
        )
        reader.assert_called_once()
        self.assertEqual(DayClose.objects.get().store, store("Globo"))

    def test_a_forged_or_expired_token_is_ignored(self):
        reader = self.fake_ocr()
        data = self.client.post(READ, {"photo": upload()}).json()
        for token in ("abc", data["token"][:-3] + "xyz", ""):
            with self.subTest(token=token):
                reader.reset_mock()
                DayClose.objects.all().delete()
                self.save({"receipts_total": "", "photo": upload(), "reading_token": token})
                reader.assert_called_once()
        reader.reset_mock()
        DayClose.objects.all().delete()
        with mock.patch.object(ocr, "TOKEN_MAX_AGE", -1):
            self.save({"receipts_total": "", "photo": upload(), "reading_token": data["token"]})
        reader.assert_called_once()

    def test_a_token_cannot_change_the_amount_the_user_typed(self):
        self.fake_ocr()
        data = self.client.post(READ, {"photo": upload()}).json()
        self.save({"receipts_total": "500", "photo": upload(), "reading_token": data["token"]})
        close = DayClose.objects.get()
        self.assertEqual((close.receipts_total, close.detected_total, close.reading), (D("500.00"), D("947.50"), "corrected"))


class ReadEndpointTests(ReceiptCase):
    def test_it_needs_a_login_and_a_role(self):
        self.client.logout()
        self.assertEqual(self.client.post(READ, {"photo": upload()}).status_code, 302)
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.post(READ, {"photo": upload()}).status_code, 403)

    def test_it_needs_post_and_the_csrf_token(self):
        self.assertEqual(self.client.get(READ).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(READ, {"photo": upload()}).status_code, 403)

    def test_without_a_photo_or_with_a_bad_one_it_is_a_400(self):
        self.assertEqual(self.client.post(READ).status_code, 400)
        bogus = SimpleUploadedFile("x.jpg", b"non sono una foto", content_type="image/jpeg")
        response = self.client.post(READ, {"photo": bogus})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    @override_settings(MAX_UPLOAD_MB=0)
    def test_a_photo_over_the_limit_is_a_400(self):
        self.assertEqual(self.client.post(READ, {"photo": upload()}).status_code, 400)

    def test_with_the_reading_off_it_says_so(self):
        response = self.client.post(READ, {"photo": upload()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"available": False})

    def test_it_returns_the_amount_the_line_and_a_signed_token(self):
        self.fake_ocr()
        photo = upload()
        response = self.client.post(READ, {"photo": photo})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["available"], data["amount"], data["line"]), (True, "947,50", "TOTALE COMPLESSIVO 947,50"))
        valid, reading = ocr.verify_reading(data["token"], self.user.pk, processed_bytes(photo))
        self.assertEqual((valid, reading), (True, READING))

    def test_an_unreadable_photo_still_gives_a_token(self):
        self.fake_ocr(reading=None)
        photo = upload()
        data = self.client.post(READ, {"photo": photo}).json()
        self.assertEqual((data["available"], data["amount"], data["line"]), (True, None, ""))
        self.assertEqual(ocr.verify_reading(data["token"], self.user.pk, processed_bytes(photo)), (True, None))

    def test_the_answer_is_never_cached(self):
        self.fake_ocr()
        response = self.client.post(READ, {"photo": upload()})
        self.assertIn("no-store", response["Cache-Control"])

    def test_nothing_is_saved(self):
        self.fake_ocr()
        self.client.post(READ, {"photo": upload()})
        self.assertFalse(DayClose.objects.exists())


# --- La foto e l'eliminazione -----------------------------------------------------------


class PhotoViewTests(ReceiptCase):
    def test_a_manager_sees_the_photo_of_a_close_of_the_own_center(self):
        close = make_close_with_photo()
        response = self.client.get(reverse("inventory:receipt_photo", args=[close.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("private", response["Cache-Control"])
        body = b"".join(response.streaming_content)
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_the_photo_of_another_center_is_a_404_for_a_manager(self):
        other = make_close_with_photo(store_name="Globo")
        self.assertEqual(self.client.get(reverse("inventory:receipt_photo", args=[other.pk])).status_code, 404)

    def test_an_owner_sees_every_photo(self):
        self.as_owner()
        for name in ("Milanofiori", "Globo"):
            close = make_close_with_photo(store_name=name)
            response = self.client.get(reverse("inventory:receipt_photo", args=[close.pk]))
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_a_close_without_a_photo_or_with_a_lost_file_is_a_404(self):
        bare = make_close(timezone.localdate(), "10")
        self.assertEqual(self.client.get(reverse("inventory:receipt_photo", args=[bare.pk])).status_code, 404)
        lost = make_close_with_photo(timezone.localdate() - timedelta(days=1))
        lost.photo.storage.delete(lost.photo.name)
        self.assertEqual(self.client.get(reverse("inventory:receipt_photo", args=[lost.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("inventory:receipt_photo", args=[999999])).status_code, 404)

    def test_the_photo_needs_a_login_and_only_get(self):
        close = make_close_with_photo()
        url = reverse("inventory:receipt_photo", args=[close.pk])
        self.assertEqual(self.client.post(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_media_address_of_the_file_is_not_served(self):
        close = make_close_with_photo()
        self.assertEqual(self.client.get(f"/media/{close.photo.name}").status_code, 404)


class DeleteTests(ReceiptCase):
    def url(self, close):
        return reverse("inventory:close_delete", args=[close.pk])

    def test_a_manager_deletes_a_close_of_today_with_its_photo(self):
        close = make_close_with_photo()
        name = close.photo.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url(close))
        self.assertRedirects(response, RECEIPTS, fetch_redirect_response=False)
        self.assertFalse(DayClose.objects.exists())
        self.assertFalse(close.photo.storage.exists(name))

    def test_deleting_needs_post(self):
        close = make_close(timezone.localdate(), "10")
        self.assertEqual(self.client.get(self.url(close)).status_code, 405)
        self.assertTrue(DayClose.objects.exists())

    def test_a_manager_cannot_delete_an_old_day_or_another_center(self):
        old = make_close(timezone.localdate() - timedelta(days=3), "10")
        other = make_close(timezone.localdate(), "10", store_name="Globo")
        self.assertEqual(self.client.post(self.url(old)).status_code, 403)
        self.assertEqual(self.client.post(self.url(other)).status_code, 404)
        self.assertEqual(DayClose.objects.count(), 2)

    def test_an_owner_deletes_any_day_and_returns_to_that_center(self):
        self.as_owner()
        old = make_close(timezone.localdate() - timedelta(days=40), "10", store_name="Globo")
        response = self.client.post(self.url(old))
        self.assertRedirects(response, f"{RECEIPTS}?centro={store('Globo').pk}", fetch_redirect_response=False)
        self.assertFalse(DayClose.objects.exists())

    def test_deleting_needs_a_login_and_a_role(self):
        close = make_close(timezone.localdate(), "10")
        self.client.logout()
        self.assertEqual(self.client.post(self.url(close)).status_code, 302)
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.post(self.url(close)).status_code, 403)
        self.assertTrue(DayClose.objects.exists())


# --- L'elenco e le tre medie -------------------------------------------------------------


class ReceiptsPageTests(ReceiptCase):
    def setUp(self):
        super().setUp()
        self.freeze_today()

    def fill(self):
        """Milanofiori: lun 100 (+20 Yakuza), mar 200, sab 400, dom 600. Globo: mar 300."""
        make_close(MON_5, 100, 20)
        make_close(TUE_6, 200)
        make_close(SAT_10, 400)
        make_close(SUN_11, 600)
        make_close(TUE_6, 300, store_name="Globo")

    def test_a_manager_sees_the_averages_of_the_own_center_without_yakuza(self):
        self.fill()
        response = self.client.get(RECEIPTS)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "inventory/receipts.html")
        average = response.context["average"]
        self.assertEqual(
            (average.weekdays.avg_total, average.weekend.avg_total, average.all_days.avg_total, average.week_total),
            (D("150.00"), D("500.00"), D("325.00"), D("1300.00")),
        )
        self.assertEqual(average.weekdays.yakuza, D("0"))
        self.assertIsNone(response.context["by_store"])
        for text in ("Infrasettimanale", "Settimanale", "Fine settimana", "150,00 €", "500,00 €", "325,00 €", "1.300,00 €"):
            self.assertContains(response, text)

    def test_a_manager_never_sees_yakuza_figures_on_this_page(self):
        self.fill()
        response = self.client.get(RECEIPTS)
        self.assertNotContains(response, "Yakuza")
        self.assertNotContains(response, "20,00")
        self.assertNotContains(response, "120,00")
        self.assertTrue(all(close.yakuza_cash == 0 for close in response.context["closes"]))
        self.assertEqual([close.day_total for close in response.context["closes"]][-1], D("100.00"))

    def test_the_three_averages_are_drawn_as_bars(self):
        self.fill()
        response = self.client.get(RECEIPTS)
        chart = response.context["chart"]
        # manager: lun–ven 150, tutti 325, sab–dom 500 (senza Yakuza)
        self.assertEqual([r.pct for r in chart], [30, 65, 100])
        self.assertContains(response, 'style="--w: 100%"')
        self.assertContains(response, 'style="--w: 30%"')
        self.assertNotContains(response, "avg-seg-yak")
        self.assertContains(response, "Una settimana intera: <b>1.300,00 €</b>", html=False)
        self.as_owner()
        owner = self.client.get(RECEIPTS, {"centro": store("Milanofiori").pk})
        self.assertContains(owner, "avg-seg-yak")
        self.assertContains(owner, '<span class="swatch swatch-yak"></span>Yakuza', html=False)

    def test_a_manager_sees_only_the_own_center(self):
        self.fill()
        response = self.client.get(RECEIPTS, {"centro": store("Globo").pk})
        self.assertEqual(response.context["store"], store("Milanofiori"))
        self.assertEqual({close.store for close in response.context["closes"]}, {store("Milanofiori")})
        self.assertEqual(len(response.context["closes"]), 4)

    def test_an_owner_sees_all_centers_together_and_one_by_one(self):
        self.fill()
        self.as_owner()
        response = self.client.get(RECEIPTS)
        self.assertIsNone(response.context["store"])
        average = response.context["average"]
        # martedì 6 vale 200 + 300: un solo giorno con i due centri insieme
        self.assertEqual((average.weekdays.days, average.weekdays.avg_total), (2, D("310.00")))
        self.assertEqual(average.weekdays.avg_yakuza, D("10.00"))
        by_store = {item.name: avg for item, avg in response.context["by_store"]}
        self.assertEqual(set(by_store), {"Milanofiori", "Globo", "Tor Vergata"})
        self.assertEqual(by_store["Milanofiori"].weekdays.avg_total, D("160.00"))
        self.assertEqual(by_store["Globo"].weekdays.avg_total, D("300.00"))
        self.assertFalse(by_store["Tor Vergata"].has_data)
        self.assertContains(response, "Per centro")

    def test_an_owner_sees_the_yakuza_breakdown(self):
        self.fill()
        self.as_owner()
        response = self.client.get(RECEIPTS, {"centro": store("Milanofiori").pk})
        self.assertContains(response, "Yakuza")
        self.assertContains(response, "scontrini 100,00 € + Yakuza 20,00 €")
        self.assertContains(response, "120,00 €")  # la giornata: scontrini più Yakuza
        average = response.context["average"]
        self.assertEqual((average.weekdays.avg_total, average.week_total), (D("160.00"), D("1320.00")))
        self.assertIsNone(response.context["by_store"])

    def test_the_period_limits_the_closes(self):
        make_close(TODAY - timedelta(days=3), 100)
        make_close(TODAY - timedelta(days=40), 900)
        self.assertEqual(self.client.get(RECEIPTS).context["average"].all_days.days, 2)  # 3 mesi
        short = self.client.get(RECEIPTS, {"periodo": "14g"})
        self.assertEqual((short.context["period"].key, short.context["average"].all_days.days), ("14g", 1))
        for raw in ("oggi", "x", "7g"):
            self.assertEqual(self.client.get(RECEIPTS, {"periodo": raw}).context["period"].key, averages.DEFAULT_PERIOD)
        forever = self.client.get(RECEIPTS, {"periodo": "sempre"})
        self.assertEqual(forever.context["average"].all_days.days, 2)

    def test_the_period_links_keep_the_center(self):
        self.as_owner()
        response = self.client.get(RECEIPTS, {"centro": store("Globo").pk, "periodo": "30g"})
        self.assertContains(response, f"centro={store('Globo').pk}&amp;periodo=14g")

    def test_the_days_are_listed_newest_first_with_how_the_amount_was_obtained(self):
        make_close(TODAY, 947.5, detected_total=D("947.50"))
        make_close(YESTERDAY, 940, detected_total=D("947.50"))
        make_close(TODAY - timedelta(days=2), 80)
        response = self.client.get(RECEIPTS)
        self.assertEqual([close.date for close in response.context["closes"]], [TODAY, YESTERDAY, TODAY - timedelta(days=2)])
        self.assertContains(response, "Letto dalla foto")
        self.assertContains(response, "Corretto a mano · letto 947,50 €")
        self.assertContains(response, "Scritto a mano")

    def test_the_photo_link_is_only_there_when_there_is_a_photo(self):
        with_photo = make_close_with_photo(TODAY)
        make_close(YESTERDAY, 10)
        response = self.client.get(RECEIPTS)
        self.assertContains(response, reverse("inventory:receipt_photo", args=[with_photo.pk]))
        self.assertEqual(response.content.decode().count("/foto/"), 1)

    def test_edit_links_follow_the_edit_window(self):
        make_close(TODAY, 10)
        make_close(YESTERDAY, 10)
        make_close(TODAY - timedelta(days=3), 10)
        links = {close.date: close.edit_url for close in self.client.get(RECEIPTS).context["closes"]}
        self.assertEqual(links[TODAY], f"{CLOSE}?data={TODAY.isoformat()}")
        self.assertEqual(links[YESTERDAY], f"{CLOSE}?data={YESTERDAY.isoformat()}")
        self.assertIsNone(links[TODAY - timedelta(days=3)])
        self.as_owner()
        owner_links = {(c.store_id, c.date): c.edit_url for c in self.client.get(RECEIPTS).context["closes"]}
        self.assertTrue(all(url and f"centro={store('Milanofiori').pk}" in url for url in owner_links.values()))

    def test_the_list_is_limited(self):
        DayClose.objects.bulk_create(
            DayClose(store=store(), date=TODAY - timedelta(days=n), receipts_total=D("10")) for n in range(averages.LIST_LIMIT + 5)
        )
        response = self.client.get(RECEIPTS, {"periodo": "sempre"})
        self.assertEqual(len(response.context["closes"]), averages.LIST_LIMIT)
        self.assertTrue(response.context["more"])
        self.assertContains(response, f"ultime {averages.LIST_LIMIT} giornate")
        # le medie usano tutte le giornate, non solo quelle dell'elenco
        self.assertEqual(response.context["average"].all_days.days, averages.LIST_LIMIT + 5)

    def test_the_add_button_goes_to_the_form_of_the_center(self):
        self.assertEqual(self.client.get(RECEIPTS).context["add_url"], CLOSE)
        self.as_owner()
        self.assertEqual(self.client.get(RECEIPTS).context["add_url"], f"{CLOSE}?nuova=1")
        self.assertEqual(
            self.client.get(RECEIPTS, {"centro": store("Globo").pk}).context["add_url"],
            f"{CLOSE}?centro={store('Globo').pk}&nuova=1",
        )

    def test_with_no_closes_there_is_an_invitation(self):
        response = self.client.get(RECEIPTS)
        self.assertContains(response, "Nessuna chiusura in questo periodo")
        self.assertFalse(response.context["average"].has_data)
        self.assertEqual(response.context["closes"], [])

    def test_the_page_needs_a_login_and_a_role(self):
        self.client.logout()
        self.assertEqual(self.client.get(RECEIPTS).status_code, 302)
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.get(RECEIPTS).status_code, 403)

    def test_the_dock_links_to_the_section_for_both_roles(self):
        self.assertContains(self.client.get(RECEIPTS), f'href="{RECEIPTS}"')
        self.as_owner()
        self.assertContains(self.client.get(reverse("inventory:dashboard")), f'href="{RECEIPTS}"')

    def test_a_bad_center_for_an_owner_means_all_centers(self):
        self.fill()
        self.as_owner()
        for raw in ("abc", "99999", ""):
            with self.subTest(raw=raw):
                response = self.client.get(RECEIPTS, {"centro": raw})
                self.assertIsNone(response.context["store"])
                self.assertEqual(response.status_code, 200)


class ClosingPageYakuzaTests(ReceiptCase):
    def test_the_cash_card_shows_yakuza_to_owners_only(self):
        close = make_close(timezone.localdate(), "100", "35")
        page = self.client.get(reverse("inventory:closing"))
        self.assertContains(page, "<strong>100,00 €</strong>")
        self.assertNotContains(page, "più contanti Yakuza")
        self.assertEqual(page.context["close"].yakuza_cash, D("0.00"))
        self.as_owner()
        page = self.client.get(reverse("inventory:closing"), {"centro": close.store_id})
        self.assertContains(page, "più contanti Yakuza 35,00 €")


class LoginRequiredTests(ReceiptCase):
    def test_every_new_address_sends_anonymous_users_to_the_login(self):
        close = make_close_with_photo()
        self.client.logout()
        urls = [
            ("get", RECEIPTS),
            ("get", CLOSE),
            ("post", CLOSE),
            ("post", READ),
            ("get", reverse("inventory:receipt_photo", args=[close.pk])),
            ("post", reverse("inventory:close_delete", args=[close.pk])),
        ]
        for method, url in urls:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))
        self.assertTrue(DayClose.objects.exists())
