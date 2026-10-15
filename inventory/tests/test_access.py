"""Chi vede cosa: ogni store manager lavora solo sul proprio centro, i titolari vedono tutto."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import Batch, DayClose, Package, Profile
from .helpers import (
    TEMP_MEDIA,
    BaseCase,
    close_day,
    create_package,
    make_batch,
    make_user,
    sell_on,
    store,
)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ScopeCase(TestCase):
    """Due centri con i loro dati, un manager per centro, un titolare e un utente senza ruolo."""

    @classmethod
    def setUpTestData(cls):
        cls.mario = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        cls.lucia = make_user("lucia", Profile.Role.MANAGER, "Globo")
        cls.tom = make_user("tom", Profile.Role.OWNER)
        cls.nobody = make_user("nessuno")
        cls.boss = get_user_model().objects.create_superuser("capo", password="una-password-lunga-123")

        cls.batch_m = make_batch("Lotto M", 3, "Milanofiori")
        cls.batch_g = make_batch("Lotto G", 3, "Globo")
        cls.pkg_m = create_package("1.000", cls.batch_m)
        cls.pkg_g = create_package("2.000", cls.batch_g)
        cls.sold_m = create_package("1.500", cls.batch_m, sold_price="37.50")
        cls.sold_g = create_package("2.500", cls.batch_g, sold_price="62.50")

    def login(self, user):
        self.client.force_login(user)


class ManagerIsolationTests(ScopeCase):
    def setUp(self):
        self.login(self.mario)

    def other_store_urls(self):
        g, bg = self.pkg_g, self.batch_g
        return [
            ("get", reverse("inventory:detail", args=[g.pk])),
            ("get", reverse("inventory:update", args=[g.pk])),
            ("post", reverse("inventory:update", args=[g.pk])),
            ("get", reverse("inventory:sell", args=[g.pk])),
            ("post", reverse("inventory:sell", args=[g.pk])),
            ("post", reverse("inventory:restore", args=[self.sold_g.pk])),
            ("get", reverse("inventory:delete", args=[g.pk])),
            ("post", reverse("inventory:delete", args=[g.pk])),
            ("post", reverse("inventory:closing_delete", args=[self.sold_g.pk])),
            ("get", reverse("inventory:image", args=[g.pk, "foto"])),
            ("get", reverse("inventory:image", args=[g.pk, "miniatura"])),
            ("get", reverse("inventory:batch_update", args=[bg.pk])),
            ("post", reverse("inventory:batch_update", args=[bg.pk])),
            ("get", reverse("inventory:store", args=[store("Globo").pk])),
            ("get", reverse("inventory:store", args=[store("Tor Vergata").pk])),
        ]

    def test_every_object_of_another_center_is_a_404(self):
        for method, url in self.other_store_urls():
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, {"weight_kg": "1", "name": "x", "sold_price": "1"})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(Package.objects.count(), 4)

    def test_another_centers_data_is_untouched_after_those_attempts(self):
        for method, url in self.other_store_urls():
            getattr(self.client, method)(url, {"weight_kg": "9", "name": "x", "sold_price": "1"})
        self.pkg_g.refresh_from_db()
        self.assertEqual(self.pkg_g.weight_kg, Decimal("2.000"))
        self.assertFalse(self.pkg_g.is_sold)
        self.assertTrue(Package.objects.filter(pk=self.sold_g.pk, sold_at__isnull=False).exists())

    def test_own_objects_are_reachable(self):
        for name in ("detail", "update", "sell", "delete"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"inventory:{name}", args=[self.pkg_m.pk])).status_code, 200)
        response = self.client.get(reverse("inventory:image", args=[self.pkg_m.pk, "foto"]))
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_stock_history_and_lots_show_only_own_center(self):
        stock = self.client.get(reverse("inventory:list"))
        self.assertEqual([p.pk for p in stock.context["packages"]], [self.pkg_m.pk])
        history = self.client.get(reverse("inventory:history"))
        self.assertEqual([p.pk for p in history.context["packages"]], [self.sold_m.pk])
        lots = self.client.get(reverse("inventory:batch_list"))
        self.assertEqual([b.pk for b in lots.context["batches"]], [self.batch_m.pk])
        self.assertNotContains(lots, "Lotto G")

    def test_filtering_by_a_lot_of_another_center_finds_nothing(self):
        response = self.client.get(reverse("inventory:list"), {"batch": self.batch_g.pk})
        self.assertEqual(len(response.context["packages"]), 0)
        self.assertIn("batch", response.context["form"].errors)

    def test_search_form_has_no_center_filter_for_managers(self):
        response = self.client.get(reverse("inventory:list"))
        self.assertNotIn("centro", response.context["form"].fields)
        # e un ?centro= nell'indirizzo non allarga nulla
        response = self.client.get(reverse("inventory:list"), {"centro": store("Globo").pk})
        self.assertEqual([p.pk for p in response.context["packages"]], [self.pkg_m.pk])

    def test_cannot_register_a_package_in_a_lot_of_another_center(self):
        response = self.client.post(reverse("inventory:create"), {"weight_kg": "1", "batch": self.batch_g.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn("batch", response.context["form"].errors)
        self.assertEqual(Package.objects.count(), 4)

    def test_cannot_move_a_package_to_another_center_by_posting_a_store(self):
        response = self.client.post(
            reverse("inventory:create"), {"weight_kg": "1", "store": store("Globo").pk, "batch": ""}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Package.objects.order_by("-pk").first().store, store("Milanofiori"))
        self.client.post(reverse("inventory:update", args=[self.pkg_m.pk]), {"weight_kg": "1", "store": store("Globo").pk})
        self.pkg_m.refresh_from_db()
        self.assertEqual(self.pkg_m.store, store("Milanofiori"))

    def test_centro_parameter_is_ignored_for_managers(self):
        other = store("Globo").pk
        response = self.client.post(f"{reverse('inventory:create')}?centro={other}", {"weight_kg": "1", "batch": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Package.objects.order_by("-pk").first().store, store("Milanofiori"))
        response = self.client.post(f"{reverse('inventory:batch_create')}?centro={other}", {"name": "Nuovo"})
        self.assertEqual(Batch.objects.get(name="Nuovo").store, store("Milanofiori"))
        self.client.post(f"{reverse('inventory:closing')}?centro={other}", {"azione": "chiusura", "receipts_total": "10"})
        self.assertEqual(DayClose.objects.get().store, store("Milanofiori"))

    def test_managers_cannot_see_or_change_prices(self):
        self.assertEqual(self.client.get(reverse("inventory:price")).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:price"), {"price_per_kg": "1"}).status_code, 403)

    def test_own_dashboard_is_the_home_and_shows_only_own_center(self):
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "inventory/store_dashboard.html")
        self.assertEqual(response.context["store"], store("Milanofiori"))
        self.assertNotContains(response, "Globo")
        self.assertNotContains(response, "Tor Vergata")
        self.assertEqual(self.client.get(reverse("inventory:store", args=[store("Milanofiori").pk])).status_code, 200)

    def test_manager_dashboard_never_shows_yakuza_totals_payments_or_differences(self):
        sell_on(timezone.localdate(), "1.000", "25.00", payment="yakuza")
        sell_on(timezone.localdate(), "1.000", "25.00", payment="card")
        close_day(timezone.localdate(), "10")
        for url in (
            reverse("inventory:dashboard"),
            f"{reverse('inventory:dashboard')}?periodo=sempre",
            f"{reverse('inventory:dashboard')}?periodo=90g",
        ):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn("Yakuza", body)
                self.assertNotIn("Pagamenti", body)
                self.assertNotIn("ifferenza", body)
                self.assertNotIn("Sconti", body)

    def test_other_manager_pages_have_no_yakuza_totals_or_differences(self):
        # La parola compare dove il manager sceglie il pagamento, ma mai un totale Yakuza né differenze di cassa.
        sell_on(timezone.localdate(), "1.000", "25.00", payment="yakuza")
        close_day(timezone.localdate(), "10")
        for url in (reverse("inventory:closing"), reverse("inventory:history"), reverse("inventory:list")):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn("di cui Yakuza", body)
                self.assertNotIn("ifferenza", body)


class OwnerTests(ScopeCase):
    def setUp(self):
        self.login(self.tom)

    def test_owner_sees_every_center_everywhere(self):
        stock = self.client.get(reverse("inventory:list"))
        self.assertEqual({p.pk for p in stock.context["packages"]}, {self.pkg_m.pk, self.pkg_g.pk})
        history = self.client.get(reverse("inventory:history"))
        self.assertEqual({p.pk for p in history.context["packages"]}, {self.sold_m.pk, self.sold_g.pk})
        lots = self.client.get(reverse("inventory:batch_list"))
        self.assertEqual({b.pk for b in lots.context["batches"]}, {self.batch_m.pk, self.batch_g.pk})
        for package in (self.pkg_m, self.pkg_g):
            self.assertEqual(self.client.get(reverse("inventory:detail", args=[package.pk])).status_code, 200)

    def test_owner_can_filter_by_center(self):
        response = self.client.get(reverse("inventory:list"), {"centro": store("Globo").pk})
        self.assertEqual([p.pk for p in response.context["packages"]], [self.pkg_g.pk])
        history = self.client.get(reverse("inventory:history"), {"centro": store("Milanofiori").pk})
        self.assertEqual([p.pk for p in history.context["packages"]], [self.sold_m.pk])

    def test_owner_sees_the_center_of_each_package(self):
        response = self.client.get(reverse("inventory:list"))
        self.assertContains(response, "Milanofiori")
        self.assertContains(response, "Globo")

    def test_owner_home_is_the_overview_of_all_centers(self):
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertTemplateUsed(response, "inventory/dashboard.html")
        self.assertEqual([c["store"].name for c in response.context["cards"]], ["Globo", "Milanofiori", "Tor Vergata"])

    def test_owner_can_open_any_center_dashboard(self):
        for name in ("Globo", "Milanofiori", "Tor Vergata"):
            with self.subTest(name=name):
                response = self.client.get(reverse("inventory:store", args=[store(name).pk]))
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse("inventory:store", args=[99999])).status_code, 404)

    def test_owner_can_change_prices(self):
        self.assertEqual(self.client.get(reverse("inventory:price")).status_code, 200)

    def test_owner_must_pick_a_center_before_writing(self):
        for name in ("create", "batch_create", "closing"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"inventory:{name}"))
                self.assertTemplateUsed(response, "inventory/choose_store.html")
                self.assertContains(response, "Globo")
                self.assertContains(response, "?centro=")

    def test_without_a_center_nothing_is_written(self):
        before = Package.objects.count()
        self.client.post(reverse("inventory:create"), {"weight_kg": "1"})
        self.client.post(reverse("inventory:closing"), {"azione": "pacco", "weight_kg": "1"})
        self.client.post(reverse("inventory:batch_create"), {"name": "X"})
        self.assertEqual(Package.objects.count(), before)
        self.assertFalse(Batch.objects.filter(name="X").exists())

    def test_invalid_center_values_fall_back_to_the_picker(self):
        for raw in ("abc", "99999", "-1", "1;drop", ""):
            with self.subTest(raw=raw):
                response = self.client.get(reverse("inventory:create"), {"centro": raw})
                self.assertTemplateUsed(response, "inventory/choose_store.html")

    def test_owner_registers_in_the_chosen_center_and_stays_there(self):
        globo = store("Globo")
        url = f"{reverse('inventory:create')}?centro={globo.pk}"
        self.assertContains(self.client.get(url), "Globo · Busnago")
        open_lot = make_batch("Aperto", 0, "Globo")
        response = self.client.post(url, {"weight_kg": "1,2", "batch": open_lot.pk, "again": "1"})
        package = Package.objects.order_by("-pk").first()
        self.assertEqual(package.store, globo)
        self.assertEqual(package.created_by, self.tom)
        self.assertIn(f"centro={globo.pk}", response["Location"])
        self.assertIn(f"lotto={open_lot.pk}", response["Location"])

    def test_owner_cannot_use_a_lot_of_a_different_center(self):
        url = f"{reverse('inventory:create')}?centro={store('Milanofiori').pk}"
        response = self.client.post(url, {"weight_kg": "1", "batch": self.batch_g.pk})
        self.assertIn("batch", response.context["form"].errors)

    def test_lot_names_only_have_to_be_unique_within_a_center(self):
        for name in ("Milanofiori", "Globo"):
            url = f"{reverse('inventory:batch_create')}?centro={store(name).pk}"
            self.assertEqual(self.client.post(url, {"name": "Lotto X"}).status_code, 302)
        self.assertEqual(Batch.objects.filter(name="Lotto X").count(), 2)
        again = self.client.post(f"{reverse('inventory:batch_create')}?centro={store('Globo').pk}", {"name": "lotto x"})
        self.assertEqual(again.status_code, 200)
        self.assertIn("name", again.context["form"].errors)

    def test_owner_can_correct_old_sales_that_managers_cannot(self):
        old = sell_on(timezone.localdate() - timedelta(days=5), "1", "25")
        self.assertEqual(self.client.get(reverse("inventory:sell", args=[old.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("inventory:restore", args=[old.pk])).status_code, 302)


class NoRoleTests(ScopeCase):
    def test_user_without_profile_gets_no_access_anywhere(self):
        self.login(self.nobody)
        urls = [
            reverse("inventory:dashboard"),
            reverse("inventory:list"),
            reverse("inventory:history"),
            reverse("inventory:closing"),
            reverse("inventory:create"),
            reverse("inventory:batch_list"),
            reverse("inventory:price"),
            reverse("inventory:detail", args=[self.pkg_m.pk]),
            reverse("inventory:image", args=[self.pkg_m.pk, "foto"]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertTemplateUsed(response, "no_access.html")
        response = self.client.post(reverse("inventory:sell", args=[self.pkg_m.pk]), {"payment": "cash"})
        self.assertEqual(response.status_code, 403)
        self.pkg_m.refresh_from_db()
        self.assertFalse(self.pkg_m.is_sold)

    def test_user_without_profile_can_still_log_out(self):
        self.login(self.nobody)
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("inventory:list")).status_code, 302)

    def test_inactive_user_with_a_profile_gets_nothing(self):
        self.mario.is_active = False
        self.mario.save()
        self.client.force_login(self.mario)  # sessione ancora valida: l'utente disattivato non risulta autenticato
        self.assertEqual(self.client.get(reverse("inventory:list")).status_code, 302)

    def test_superuser_without_profile_works_as_owner(self):
        self.login(self.boss)
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertTemplateUsed(response, "inventory/dashboard.html")
        stock = self.client.get(reverse("inventory:list"))
        self.assertEqual({p.pk for p in stock.context["packages"]}, {self.pkg_m.pk, self.pkg_g.pk})
        self.assertEqual(self.client.get(reverse("inventory:price")).status_code, 200)

    def test_anonymous_still_goes_to_login(self):
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("login")))


class SalesWindowTests(BaseCase):
    """Lo store manager corregge le vendite di oggi e di ieri; quelle più vecchie solo i titolari."""

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.today = sell_on(today, "1", "25")
        self.yesterday = sell_on(today - timedelta(days=1), "1", "25")
        self.old = sell_on(today - timedelta(days=2), "1", "25")

    def test_manager_can_fix_today_and_yesterday(self):
        for package in (self.today, self.yesterday):
            with self.subTest(package=package.pk):
                self.assertEqual(self.client.get(reverse("inventory:update", args=[package.pk])).status_code, 200)
                self.assertEqual(self.client.post(reverse("inventory:restore", args=[package.pk])).status_code, 302)

    def test_manager_cannot_touch_older_sales(self):
        self.assertEqual(self.client.get(reverse("inventory:update", args=[self.old.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:update", args=[self.old.pk]), {"notes": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:restore", args=[self.old.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("inventory:delete", args=[self.old.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:delete", args=[self.old.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:closing_delete", args=[self.old.pk])).status_code, 403)
        self.old.refresh_from_db()
        self.assertTrue(self.old.is_sold)
        self.assertEqual(self.old.notes, "")

    def test_old_sale_detail_explains_it_is_locked(self):
        response = self.client.get(reverse("inventory:detail", args=[self.old.pk]))
        self.assertContains(response, "solo un titolare")
        self.assertNotContains(response, reverse("inventory:restore", args=[self.old.pk]))

    def test_owner_has_no_window(self):
        self.as_owner()
        self.assertEqual(self.client.post(reverse("inventory:restore", args=[self.old.pk])).status_code, 302)

    def test_window_uses_the_local_day_not_utc(self):
        # Venduto ieri alle 23:30 ora locale = ancora «ieri» anche se in UTC è già un altro giorno.
        from ..stats import start_of_day

        late = sell_on(timezone.localdate() - timedelta(days=1), "1", "25")
        late.sold_at = start_of_day(timezone.localdate()) - timedelta(minutes=30)
        late.save()
        self.assertEqual(self.client.get(reverse("inventory:update", args=[late.pk])).status_code, 200)


class ProfileTests(TestCase):
    def test_a_manager_needs_a_center(self):
        user = make_user("senza-centro")
        with self.assertRaises(ValidationError):
            Profile(user=user, role=Profile.Role.MANAGER).full_clean()

    def test_the_database_refuses_a_manager_without_a_center(self):
        user = make_user("senza-centro")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(user=user, role=Profile.Role.MANAGER, store=None)

    def test_an_owner_has_no_center(self):
        profile = Profile.objects.create(user=make_user("titolare"), role=Profile.Role.OWNER, store=store())
        profile.refresh_from_db()
        self.assertIsNone(profile.store)

    def test_a_user_has_one_profile(self):
        user = make_user("uno", Profile.Role.OWNER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(user=user, role=Profile.Role.OWNER)

    def test_a_center_with_staff_or_data_cannot_be_deleted_by_accident(self):
        make_user("m", Profile.Role.MANAGER, "Globo")
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            store("Globo").delete()
