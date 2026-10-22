"""Sezione Franchising: chi è in franchising vede e tocca solo il proprio punto, e nient'altro dell'app."""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .. import ocr
from ..access import FRANCHISE_URL_NAMES, Scope
from ..models import Batch, DayClose, Franchise, FranchiseDay, Package, Profile
from .helpers import TEMP_MEDIA, close_day, create_package, make_batch, make_image, make_user, store

D = Decimal
HOME = reverse("inventory:franchise")
DAY = reverse("inventory:franchise_day")
READ = reverse("inventory:receipt_read")


def make_franchisee(username, franchise):
    user = make_user(username)
    Profile.objects.create(user=user, role=Profile.Role.FRANCHISE, franchise=franchise)
    return user


def make_day(franchise, day, receipts="100", yakuza="0", **extra):
    return FranchiseDay.objects.create(
        franchise=franchise, date=day, receipts_total=D(str(receipts)), yakuza_cash=D(str(yakuza)), **extra
    )


def with_photo(franchise_day):
    buffer = BytesIO()
    Image.new("RGB", (80, 120), "navy").save(buffer, "JPEG")
    franchise_day.photo.save("x.jpg", ContentFile(buffer.getvalue()), save=True)
    return franchise_day


@override_settings(MEDIA_ROOT=TEMP_MEDIA, OCR_ENABLED=False)
class FranchiseCase(TestCase):
    """Due punti (Napoli, Torino) con un utente ciascuno, uno store manager, un titolare, e dati dei centri."""

    @classmethod
    def setUpTestData(cls):
        cls.napoli = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        cls.torino = Franchise.objects.create(name="Punto Torino", city="Torino")
        cls.anna = make_franchisee("anna", cls.napoli)
        cls.bruno = make_franchisee("bruno", cls.torino)
        cls.mario = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        cls.tom = make_user("tom", Profile.Role.OWNER)
        cls.today = timezone.localdate()
        cls.batch = make_batch("Lotto M", 3, "Milanofiori")
        cls.package = create_package("1.000", cls.batch)
        cls.sold = create_package("1.500", cls.batch, sold_price="37.50")
        cls.close = close_day(cls.today - timedelta(days=1), "300")

    def setUp(self):
        self.client.force_login(self.anna)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]


class RoleTests(FranchiseCase):
    def test_the_franchise_role_needs_a_franchise(self):
        user = make_user("senza")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(user=user, role=Profile.Role.FRANCHISE)
        profile = Profile(user=user, role=Profile.Role.FRANCHISE)
        with self.assertRaises(ValidationError) as error:
            profile.full_clean()
        self.assertIn("franchise", error.exception.message_dict)

    def test_each_role_keeps_only_its_own_link(self):
        user = make_user("misto")
        profile = Profile.objects.create(user=user, role=Profile.Role.FRANCHISE, franchise=self.napoli, store=store())
        self.assertIsNone(profile.store)
        profile.role, profile.store = Profile.Role.MANAGER, store("Globo")
        profile.save()
        self.assertIsNone(profile.franchise)
        profile.role = Profile.Role.OWNER
        profile.save()
        self.assertEqual((profile.store, profile.franchise), (None, None))

    def test_the_scope_of_a_franchisee_is_only_the_own_franchise(self):
        scope = Scope.for_user(self.anna)
        self.assertEqual((scope.is_owner, scope.is_franchise, scope.store, scope.franchise), (False, True, None, self.napoli))
        for name in ("stores", "packages", "batches", "closes"):
            with self.subTest(name=name):
                self.assertFalse(getattr(scope, name)().exists())
        self.assertEqual(list(scope.franchises()), [self.napoli])
        make_day(self.torino, self.today)
        self.assertFalse(scope.franchise_days().exists())
        self.assertEqual(scope.resolve_franchise(str(self.torino.pk)), self.napoli)  # ?punto= non conta
        self.assertIsNone(scope.earliest_editable_day())

    def test_managers_see_no_franchise_and_owners_see_them_all(self):
        make_day(self.napoli, self.today)
        manager = Scope.for_user(self.mario)
        self.assertFalse(manager.sees_franchising)
        self.assertFalse(manager.franchises().exists())
        self.assertFalse(manager.franchise_days().exists())
        self.assertIsNone(manager.resolve_franchise(str(self.napoli.pk)))
        owner = Scope.for_user(self.tom)
        self.assertTrue(owner.sees_franchising)
        self.assertEqual(owner.franchises().count(), 2)
        self.assertEqual(owner.resolve_franchise(str(self.torino.pk)), self.torino)
        self.assertIsNone(owner.resolve_franchise("x"))


class FranchiseeIsolationTests(FranchiseCase):
    def other_urls(self):
        p, s = self.package.pk, self.sold.pk
        return [
            reverse("inventory:dashboard"),
            reverse("inventory:store", args=[store().pk]),
            reverse("inventory:closing"),
            reverse("inventory:closing_delete", args=[s]),
            reverse("inventory:receipts"),
            reverse("inventory:close_day"),
            reverse("inventory:receipt_photo", args=[self.close.pk]),
            reverse("inventory:close_delete", args=[self.close.pk]),
            reverse("inventory:list"),
            reverse("inventory:history"),
            reverse("inventory:create"),
            reverse("inventory:batch_list"),
            reverse("inventory:batch_create"),
            reverse("inventory:batch_update", args=[self.batch.pk]),
            reverse("inventory:price"),
            reverse("inventory:detail", args=[p]),
            reverse("inventory:update", args=[p]),
            reverse("inventory:sell", args=[p]),
            reverse("inventory:restore", args=[s]),
            reverse("inventory:delete", args=[p]),
            reverse("inventory:image", args=[p, "foto"]),
        ]

    def test_every_page_outside_the_franchise_section_sends_back_to_it(self):
        for url in self.other_urls():
            for method in ("get", "post"):
                with self.subTest(method=method, url=url):
                    response = getattr(self.client, method)(url, {"weight_kg": "9", "receipts_total": "1", "name": "x"})
                    self.assertRedirects(response, HOME, fetch_redirect_response=False)
        # niente è cambiato nei centri
        self.assertEqual((Package.objects.count(), Batch.objects.count(), DayClose.objects.count()), (2, 1, 1))
        self.package.refresh_from_db()
        self.assertEqual(self.package.weight_kg, D("1.000"))

    def test_the_allowed_pages_are_only_those_of_the_section(self):
        self.assertEqual(
            FRANCHISE_URL_NAMES, {"franchise", "franchise_day", "franchise_photo", "franchise_delete", "receipt_read"}
        )

    def test_the_admin_stays_closed(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_the_home_after_login_is_the_franchise_page(self):
        self.client.logout()
        response = self.client.post(reverse("login"), {"username": "anna", "password": "una-password-lunga-123"}, follow=True)
        self.assertEqual(response.request["PATH_INFO"], HOME)

    def test_the_page_shows_only_the_own_franchise(self):
        make_day(self.napoli, self.today, "120")
        make_day(self.torino, self.today, "999")
        response = self.client.get(HOME, {"punto": self.torino.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["franchise"], self.napoli)
        self.assertEqual([day.franchise for day in response.context["days"]], [self.napoli])
        for text in ("Punto Torino", "999", "Milanofiori", "Globo", "Tor Vergata", "Magazzino"):
            self.assertNotContains(response, text)
        self.assertNotContains(response, 'class="dock"')
        self.assertContains(response, "Punto Napoli")

    def test_another_franchise_day_does_not_exist(self):
        other = with_photo(make_day(self.torino, self.today))
        self.assertEqual(self.client.get(reverse("inventory:franchise_photo", args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("inventory:franchise_delete", args=[other.pk])).status_code, 404)
        self.client.post(f"{DAY}?punto={self.torino.pk}&data={self.today}", {"receipts_total": "1", "day": self.today})
        other.refresh_from_db()
        self.assertEqual(other.receipts_total, D("100.00"))  # il giorno è finito su Napoli, non su Torino
        self.assertEqual(FranchiseDay.objects.get(franchise=self.napoli).receipts_total, D("1.00"))

    def test_the_reading_of_a_receipt_works_for_a_franchisee(self):
        response = self.client.post(READ, {"photo": make_image((600, 900), name="s.jpg")})
        self.assertEqual(response.json(), {"available": False})

    def test_the_dashboard_of_another_role_is_not_reachable_by_changing_the_address(self):
        response = self.client.get(reverse("inventory:dashboard"), follow=True)
        self.assertEqual(response.request["PATH_INFO"], HOME)
        self.assertNotContains(response, "Milanofiori")


class ManagerTests(FranchiseCase):
    def setUp(self):
        self.client.force_login(self.mario)

    def test_store_managers_cannot_enter_the_franchise_section(self):
        day = with_photo(make_day(self.napoli, self.today))
        self.assertEqual(self.client.get(HOME).status_code, 403)
        self.assertEqual(self.client.get(DAY).status_code, 403)
        self.assertEqual(self.client.post(DAY, {"receipts_total": "1"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("inventory:franchise_photo", args=[day.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:franchise_delete", args=[day.pk])).status_code, 403)
        self.assertEqual(FranchiseDay.objects.count(), 1)

    def test_no_franchise_link_for_managers(self):
        self.assertNotContains(self.client.get(reverse("inventory:receipts")), HOME)
        self.assertNotContains(self.client.get(reverse("inventory:dashboard")), HOME)


class OwnerTests(FranchiseCase):
    def setUp(self):
        self.client.force_login(self.tom)

    def test_owners_see_all_the_franchises_side_by_side(self):
        make_day(self.napoli, self.today, "200")
        response = self.client.get(HOME)
        self.assertTrue(response.context["overview"])
        names = [item.name for item, _, _ in response.context["cards"]]
        self.assertEqual(names, ["Punto Napoli", "Punto Torino"])
        napoli = response.context["cards"][0]
        self.assertEqual((napoli[1].all_days.avg_total, napoli[2]), (D("200.00"), self.today))
        self.assertContains(response, f"?punto={self.napoli.pk}")

    def test_owners_open_one_franchise(self):
        make_day(self.torino, self.today, "150", "50")
        response = self.client.get(HOME, {"punto": self.torino.pk})
        self.assertEqual(response.context["franchise"], self.torino)
        self.assertEqual(response.context["average"].all_days.avg_total, D("200.00"))
        self.assertContains(response, "tutti i punti")
        self.assertEqual(response.context["add_url"], f"{DAY}?nuova=1&punto={self.torino.pk}")

    def test_owners_choose_the_franchise_before_adding_a_day(self):
        response = self.client.get(DAY)
        self.assertTemplateUsed(response, "inventory/choose_franchise.html")
        self.assertContains(response, "Punto Napoli")
        old = self.today - timedelta(days=20)
        response = self.client.post(f"{DAY}?punto={self.napoli.pk}&nuova=1", {"day": old.isoformat(), "receipts_total": "80"})
        self.assertRedirects(response, f"{HOME}?punto={self.napoli.pk}", fetch_redirect_response=False)
        saved = FranchiseDay.objects.get()
        self.assertEqual((saved.franchise, saved.date, saved.closed_by), (self.napoli, old, self.tom))

    def test_franchise_money_never_enters_the_centers(self):
        make_day(self.napoli, self.today, "5000", "1000")
        receipts = self.client.get(reverse("inventory:receipts"))
        self.assertEqual(receipts.context["average"].all_days.days, 1)  # solo la chiusura di ieri del centro
        self.assertEqual(receipts.context["average"].all_days.avg_total, D("300.00"))
        home = self.client.get(reverse("inventory:dashboard"), {"periodo": "7g"})
        self.assertEqual(home.context["total"].declared, D("300.00"))
        self.assertNotContains(home, "5.000")

    def test_the_owner_home_lists_the_franchises_apart(self):
        make_day(self.napoli, self.today)
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.context["franchise_cards"], [(self.napoli, self.today), (self.torino, None)])
        self.assertContains(response, "<h2>Franchising</h2>", html=False)
        self.assertContains(response, "non entrano nei totali")
        self.assertContains(self.client.get(reverse("inventory:receipts")), HOME)

    def test_with_no_franchise_the_owner_is_told_how_to_create_one(self):
        Profile.objects.filter(role=Profile.Role.FRANCHISE).delete()
        Franchise.objects.all().delete()
        response = self.client.get(HOME)
        self.assertContains(response, "Nessun punto in franchising")
        self.assertNotContains(self.client.get(reverse("inventory:dashboard")), "<h2>Franchising</h2>", html=False)


class DayFormTests(FranchiseCase):
    def test_a_day_with_everything(self):
        old = self.today - timedelta(days=45)  # anche le giornate passate
        response = self.client.post(
            DAY,
            {
                "day": old.isoformat(),
                "receipts_total": "2632,26",
                "yakuza_cash": "120",
                "cash_total": "438,66",
                "card_total": "2193,60",
                "expenses": "42",
                "expenses_note": "  pulizie 30, cancelleria 12 ",
                "notes": "Pioveva, poca gente al mattino.",
            },
            follow=True,
        )
        self.assertEqual(response.request["PATH_INFO"], HOME)
        day = FranchiseDay.objects.get()
        self.assertEqual(
            (day.franchise, day.date, day.receipts_total, day.yakuza_cash, day.cash_total, day.card_total, day.expenses),
            (self.napoli, old, D("2632.26"), D("120.00"), D("438.66"), D("2193.60"), D("42.00")),
        )
        self.assertEqual((day.expenses_note, day.notes, day.closed_by), ("pulizie 30, cancelleria 12", "Pioveva, poca gente al mattino.", self.anna))
        self.assertEqual((day.day_total, day.net_total, day.payments_mismatch), (D("2752.26"), D("2710.26"), False))
        self.assertIn("scontrini 2632,26 € e contanti Yakuza 120,00 €", self.messages(response)[0])

    def test_only_the_receipts_are_required(self):
        self.client.post(DAY, {"day": self.today.isoformat(), "receipts_total": "10"})
        day = FranchiseDay.objects.get()
        self.assertEqual((day.cash_total, day.card_total, day.expenses, day.expenses_note, day.notes), (None, None, D("0.00"), "", ""))
        self.assertFalse(day.payments_mismatch)

    def test_the_form_has_the_franchise_fields_and_the_date(self):
        response = self.client.get(DAY)
        self.assertTemplateUsed(response, "inventory/close_day.html")
        for name in ("day", "receipts_total", "yakuza_cash", "cash_total", "card_total", "expenses", "expenses_note", "notes", "photo"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, "Giornata del franchising")
        self.assertNotContains(response, "cambia punto")  # chi è in franchising ha un solo punto

    def test_an_existing_day_fills_the_form_and_keeps_its_date(self):
        day = make_day(self.napoli, self.today - timedelta(days=3), "500", "20", cash_total=D("200"), card_total=D("300"), expenses=D("15"), notes="ok")
        url = f"{DAY}?data={day.date.isoformat()}"
        response = self.client.get(url)
        for value in ('value="500,00"', 'value="20,00"', 'value="200,00"', 'value="300,00"', 'value="15,00"'):
            self.assertContains(response, value)
        self.assertContains(response, "ok</textarea>")
        self.client.post(url, {"receipts_total": "450", "day": self.today.isoformat()})
        day.refresh_from_db()
        self.assertEqual((day.date, day.receipts_total, day.cash_total), (self.today - timedelta(days=3), D("450.00"), None))

    def test_a_closed_day_is_never_overwritten(self):
        existing = make_day(self.napoli, self.today, "500")
        response = self.client.post(f"{DAY}?nuova=1", {"day": self.today.isoformat(), "receipts_total": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("già chiuso", response.context["form"].errors["day"][0])
        existing.refresh_from_db()
        self.assertEqual(existing.receipts_total, D("500.00"))

    def test_bad_values_are_refused(self):
        cases = [
            {"cash_total": "-1"},
            {"card_total": "abc"},
            {"expenses": "-5"},
            {"expenses": "10000000"},
            {"notes": "x" * 2001},
            {"expenses_note": "x" * 201},
        ]
        for extra in cases:
            with self.subTest(extra=list(extra)):
                response = self.client.post(DAY, {"day": self.today.isoformat(), "receipts_total": "10", **extra})
                self.assertEqual(response.status_code, 200)
                self.assertIn(next(iter(extra)), response.context["form"].errors)
        self.assertFalse(FranchiseDay.objects.exists())

    def test_the_future_is_refused(self):
        response = self.client.post(DAY, {"day": (self.today + timedelta(days=1)).isoformat(), "receipts_total": "10"})
        self.assertIn("nel futuro", response.context["form"].errors["day"][0])

    def test_the_photo_is_read_and_the_reading_is_not_repeated(self):
        reading = ocr.Reading(D("2632.26"), "Totale Contabile 2632,26")
        with mock.patch.object(ocr, "available", return_value=True), mock.patch.object(ocr, "read_total", return_value=reading) as reader:
            token = self.client.post(READ, {"photo": make_image((600, 900), name="s.jpg")}).json()["token"]
            reader.reset_mock()
            self.client.post(
                DAY, {"day": self.today.isoformat(), "receipts_total": "", "photo": make_image((600, 900), name="s.jpg"), "reading_token": token}
            )
            reader.assert_not_called()
        day = FranchiseDay.objects.get()
        self.assertEqual((day.receipts_total, day.detected_total, day.reading, day.has_photo), (D("2632.26"), D("2632.26"), "read", True))
        self.assertRegex(day.photo.name, r"^receipts/\d{4}/\d{2}/[0-9a-f]{32}\.jpg$")

    def test_the_own_photo_is_served_privately(self):
        day = with_photo(make_day(self.napoli, self.today))
        response = self.client.get(reverse("inventory:franchise_photo", args=[day.pk]))
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("private", response["Cache-Control"])
        response.close()

    def test_deleting_a_day_removes_its_photo(self):
        day = with_photo(make_day(self.napoli, self.today - timedelta(days=30)))
        name = day.photo.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("inventory:franchise_delete", args=[day.pk]))
        self.assertRedirects(response, HOME, fetch_redirect_response=False)
        self.assertFalse(FranchiseDay.objects.exists())
        self.assertFalse(day.photo.storage.exists(name))
        self.assertEqual(self.client.get(reverse("inventory:franchise_delete", args=[1])).status_code, 405)


class PageTests(FranchiseCase):
    def test_averages_totals_and_days(self):
        monday = self.today - timedelta(days=self.today.weekday() + 7)  # lunedì della settimana scorsa
        make_day(self.napoli, monday, "100", "20", cash_total=D("60"), card_total=D("40"), expenses=D("30"), expenses_note="pulizie")
        make_day(self.napoli, monday + timedelta(days=5), "300", cash_total=D("100"), card_total=D("150"), notes="Sabato pieno")
        response = self.client.get(HOME)
        average = response.context["average"]
        self.assertEqual((average.weekdays.avg_total, average.weekend.avg_total), (D("120.00"), D("300.00")))
        totals = response.context["totals"]
        self.assertEqual((totals["income"], totals["expenses"], totals["net"]), (D("420.00"), D("30.00"), D("390.00")))
        self.assertEqual((totals["cash"], totals["card"], totals["days"]), (D("160.00"), D("190.00"), 2))
        for text in ("Infrasettimanale", "Fine settimana", "avg-seg-yak", "Incasso meno spese", "390,00 €", "pulizie", "Sabato pieno"):
            self.assertContains(response, text)
        self.assertContains(response, "Contanti + carta ≠ scontrini")  # 100 + 150 non fa 300
        self.assertContains(response, f"{DAY}?data={monday.isoformat()}")

    def test_spending_more_than_the_income_shows_in_red(self):
        make_day(self.napoli, self.today, "10", expenses=D("50"))
        response = self.client.get(HOME)
        self.assertEqual(response.context["totals"]["net"], D("-40.00"))
        self.assertContains(response, 'class="is-warn"')

    def test_an_empty_franchise_invites_to_add_a_day(self):
        response = self.client.get(HOME)
        self.assertContains(response, "Nessuna giornata in questo periodo")
        self.assertContains(response, f'href="{DAY}?nuova=1"')

    def test_the_page_needs_a_login(self):
        self.client.logout()
        for url in (HOME, DAY):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response["Location"].startswith(reverse("login")))
