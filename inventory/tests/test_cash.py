"""Contanti, carta ed eccesso cassa: lo Yakuza è l'eccesso cassa meno i contanti già battuti sullo scontrino.

Prima lo Yakuza si scriveva come eccesso cassa e veniva sommato agli scontrini: i contanti battuti
contavano due volte (negli scontrini e nell'eccesso). Qui si controlla che non succeda più, nei centri e
nel franchising, da qualunque parte arrivi il salvataggio, e che un ammanco di cassa venga segnalato.
"""
from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import DayClose, Franchise, FranchiseDay, Profile
from .helpers import TEMP_MEDIA, make_user, store

CLOSE = reverse("inventory:close_day")
RECEIPTS = reverse("inventory:receipts")
CLOSING = reverse("inventory:closing")
FRANCHISE = reverse("inventory:franchise")
FRANCHISE_DAY = reverse("inventory:franchise_day")


class YakuzaCalculationTests(TestCase):
    def close(self, **values):
        return DayClose.objects.create(store=store(), date=timezone.localdate(), receipts_total=D("300"), **values)

    def test_yakuza_is_the_cash_excess_minus_the_cash_on_the_receipt(self):
        # scontrini 300 (100 contanti + 200 carta), in cassa 150 oltre i 250: 100 battuti + 50 senza scontrino
        close = self.close(cash_total=D("100"), card_total=D("200"), cash_excess=D("150"))
        self.assertEqual(close.yakuza_cash, D("50.00"))
        self.assertEqual(close.day_total, D("350.00"))  # non 450: i 100 battuti non contano due volte
        self.assertEqual(close.cash_shortage, D("0"))
        self.assertFalse(close.payments_mismatch)

    def test_without_cash_on_the_receipt_the_whole_excess_is_yakuza(self):
        close = self.close(cash_excess=D("40"))
        self.assertEqual((close.yakuza_cash, close.cash_shortage), (D("40.00"), D("0")))

    def test_a_shortage_gives_zero_yakuza_and_is_measured(self):
        close = self.close(cash_total=D("80"), card_total=D("220"), cash_excess=D("60"))
        self.assertEqual((close.yakuza_cash, close.cash_shortage, close.day_total), (D("0.00"), D("20.00"), D("300.00")))

    def test_every_save_recalculates_even_a_partial_one(self):
        close = self.close(cash_total=D("100"), cash_excess=D("150"))
        close.cash_excess = D("130")
        close.save(update_fields=["cash_excess"])
        close.refresh_from_db()
        self.assertEqual(close.yakuza_cash, D("30.00"))
        close.yakuza_cash = D("999")  # scritto a mano non vale: si ricalcola
        close.save()
        close.refresh_from_db()
        self.assertEqual(close.yakuza_cash, D("30.00"))

    def test_cash_and_card_that_do_not_add_up_are_flagged(self):
        self.assertTrue(self.close(cash_total=D("100"), card_total=D("150")).payments_mismatch)

    def test_the_same_rule_holds_for_franchise_days(self):
        franchise = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        day = FranchiseDay.objects.create(
            franchise=franchise, date=timezone.localdate(), receipts_total=D("300"), cash_total=D("100"), cash_excess=D("150")
        )
        self.assertEqual((day.yakuza_cash, day.day_total), (D("50.00"), D("350.00")))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class CloseFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = make_user("marco", Profile.Role.MANAGER, "Milanofiori")
        cls.owner = make_user("tom", Profile.Role.OWNER)

    def setUp(self):
        self.client.force_login(self.manager)

    def save(self, data, follow=False):
        return self.client.post(CLOSE, data, follow=follow)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_the_form_follows_the_receipt_then_the_till(self):
        html = self.client.get(CLOSE).content.decode()
        positions = [html.index(f'name="{name}"') for name in ("receipts_total", "cash_total", "card_total", "cash_excess")]
        self.assertEqual(positions, sorted(positions))

    def test_an_excess_needs_the_cash_on_the_receipt(self):
        response = self.save({"receipts_total": "300", "cash_excess": "150"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["cash_total"],
            ["Scrivi i contanti dello scontrino (anche 0): si tolgono dall'eccesso cassa."],
        )
        self.assertFalse(DayClose.objects.exists())

    def test_zero_cash_is_an_answer(self):
        self.assertEqual(self.save({"receipts_total": "300", "cash_total": "0", "cash_excess": "150"}).status_code, 302)
        self.assertEqual(DayClose.objects.get().yakuza_cash, D("150.00"))

    def test_without_excess_the_cash_is_optional(self):
        self.save({"receipts_total": "300"})
        close = DayClose.objects.get()
        self.assertEqual((close.cash_total, close.card_total, close.cash_excess, close.yakuza_cash), (None, None, D("0.00"), D("0.00")))

    def test_a_full_close_is_counted_once(self):
        self.save({"receipts_total": "300", "cash_total": "100", "card_total": "200", "cash_excess": "150"})
        close = DayClose.objects.get()
        self.assertEqual(
            (close.receipts_total, close.cash_total, close.card_total, close.cash_excess, close.yakuza_cash, close.day_total),
            (D("300.00"), D("100.00"), D("200.00"), D("150.00"), D("50.00"), D("350.00")),
        )

    def test_reopening_a_day_shows_what_was_typed_not_the_yakuza(self):
        self.save({"receipts_total": "300", "cash_total": "100", "card_total": "200", "cash_excess": "150"})
        form = self.client.get(CLOSE).context["form"]
        self.assertEqual(
            {name: form.initial[name] for name in ("receipts_total", "cash_total", "card_total", "cash_excess")},
            {"receipts_total": D("300.00"), "cash_total": D("100.00"), "card_total": D("200.00"), "cash_excess": D("150.00")},
        )

    def test_a_shortage_is_saved_with_a_warning_to_whoever_closes(self):
        response = self.save({"receipts_total": "300", "cash_total": "80", "card_total": "220", "cash_excess": "60"}, follow=True)
        close = DayClose.objects.get()
        self.assertEqual((close.yakuza_cash, close.cash_shortage), (D("0.00"), D("20.00")))
        self.assertTrue(any(m.startswith("In cassa mancano 20,00 €") for m in self.messages(response)))
        self.assertContains(response, "notice-warning")

    def test_no_warning_when_the_till_adds_up(self):
        response = self.save({"receipts_total": "300", "cash_total": "100", "cash_excess": "100"}, follow=True)
        self.assertFalse(any("mancano" in m for m in self.messages(response)))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class WhoSeesWhatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = make_user("marco", Profile.Role.MANAGER, "Milanofiori")
        cls.owner = make_user("tom", Profile.Role.OWNER)
        today = timezone.localdate()
        cls.short = DayClose.objects.create(
            store=store(), date=today, receipts_total=D("300"), cash_total=D("80"), card_total=D("200"), cash_excess=D("60")
        )

    def test_owners_see_the_shortage_and_the_mismatch(self):
        self.client.force_login(self.owner)
        page = self.client.get(RECEIPTS, {"centro": store().pk})
        self.assertContains(page, "Ammanco cassa 20,00 €")
        self.assertContains(page, "Contanti + carta ≠ scontrini")  # 80 + 200 ≠ 300
        self.assertContains(page, "contanti 80,00 €")
        self.assertContains(self.client.get(CLOSING, {"centro": store().pk}), "Ammanco cassa 20,00 €")

    def test_managers_never_see_yakuza_or_the_shortage(self):
        self.short.cash_excess = D("130")  # ora 50 di Yakuza
        self.short.save()
        self.client.force_login(self.manager)
        for page in (self.client.get(RECEIPTS), self.client.get(CLOSING)):
            self.assertNotContains(page, "Yakuza 50")
            self.assertNotContains(page, "Ammanco")

    def test_the_admin_shows_the_yakuza_but_does_not_let_anyone_type_it(self):
        admin = get_user_model().objects.create_superuser("admin", password="una-password-lunga-123")
        self.client.force_login(admin)
        page = self.client.get(reverse("admin:inventory_dayclose_change", args=[self.short.pk]))
        self.assertContains(page, 'name="cash_excess"')
        self.assertContains(page, 'name="cash_total"')
        self.assertNotContains(page, 'name="yakuza_cash"')
        self.assertContains(page, "Yakuza")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class FranchiseCashTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.napoli = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        cls.anna = make_user("anna")
        Profile.objects.create(user=cls.anna, role=Profile.Role.FRANCHISE, franchise=cls.napoli)

    def setUp(self):
        self.client.force_login(self.anna)

    def test_the_franchise_day_subtracts_the_cash_too(self):
        today = timezone.localdate().isoformat()
        response = self.client.post(
            FRANCHISE_DAY,
            {"day": today, "receipts_total": "300", "cash_total": "100", "card_total": "200", "cash_excess": "150"},
            follow=True,
        )
        day = FranchiseDay.objects.get()
        self.assertEqual((day.yakuza_cash, day.day_total), (D("50.00"), D("350.00")))
        self.assertIn("e Yakuza 50,00 €", [str(m) for m in response.context["messages"]][0])  # il punto vede il suo

    def test_the_franchise_form_also_needs_the_cash_with_an_excess(self):
        response = self.client.post(FRANCHISE_DAY, {"day": timezone.localdate().isoformat(), "receipts_total": "300", "cash_excess": "150"})
        self.assertIn("cash_total", response.context["form"].errors)
        self.assertFalse(FranchiseDay.objects.exists())

    def test_a_shortage_shows_on_the_franchise_page(self):
        FranchiseDay.objects.create(
            franchise=self.napoli, date=timezone.localdate() - timedelta(days=1), receipts_total=D("300"),
            cash_total=D("90"), card_total=D("210"), cash_excess=D("75"),
        )
        self.assertContains(self.client.get(FRANCHISE), "Ammanco cassa 15,00 €")


class CashMigrationTests(TransactionTestCase):
    """La 0012 aggiunge contanti, carta ed eccesso cassa: lo Yakuza delle giornate già salvate non cambia."""

    serialized_rollback = True
    BEFORE = [("inventory", "0011_risorse")]
    AFTER = [("inventory", "0012_contanti_eccesso_cassa")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_saved_days_keep_their_yakuza_and_it_can_be_undone(self):
        old = self.migrate(self.BEFORE)
        Store = old.get_model("inventory", "Store")
        Franchise = old.get_model("inventory", "Franchise")
        close = old.get_model("inventory", "DayClose").objects.create(
            store=Store.objects.get(name="Globo"), date="2026-10-01", receipts_total=D("300.00"), yakuza_cash=D("40.00")
        )
        napoli = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        days = old.get_model("inventory", "FranchiseDay").objects
        with_cash = days.create(franchise=napoli, date="2026-10-01", receipts_total=D("300.00"), yakuza_cash=D("20.00"), cash_total=D("200.00"))
        without = days.create(franchise=napoli, date="2026-10-02", receipts_total=D("100.00"), yakuza_cash=D("5.00"))

        new = self.migrate(self.AFTER)
        moved = new.get_model("inventory", "DayClose").objects.get(pk=close.pk)
        self.assertEqual((moved.yakuza_cash, moved.cash_excess, moved.cash_total, moved.card_total), (D("40.00"), D("40.00"), None, None))
        NewDay = new.get_model("inventory", "FranchiseDay")
        self.assertEqual((NewDay.objects.get(pk=with_cash.pk).yakuza_cash, NewDay.objects.get(pk=with_cash.pk).cash_excess), (D("20.00"), D("220.00")))
        self.assertEqual((NewDay.objects.get(pk=without.pk).yakuza_cash, NewDay.objects.get(pk=without.pk).cash_excess), (D("5.00"), D("5.00")))
        # Con il modello di oggi, risalvare una giornata migrata ridà lo stesso Yakuza.
        current = DayClose.objects.get(pk=close.pk)
        current.save()
        current.refresh_from_db()
        self.assertEqual(current.yakuza_cash, D("40.00"))

        back = self.migrate(self.BEFORE)
        self.assertEqual(back.get_model("inventory", "DayClose").objects.get(pk=close.pk).yakuza_cash, D("40.00"))
        self.assertEqual(back.get_model("inventory", "FranchiseDay").objects.get(pk=with_cash.pk).cash_total, D("200.00"))
