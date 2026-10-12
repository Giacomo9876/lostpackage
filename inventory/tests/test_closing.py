"""Chiusura di giornata: pacchi venduti inseriti a fine giornata e totale degli scontrini in cassa."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from ..models import DayClose, Package, ShopSettings
from .helpers import BaseCase, close_day, create_package, make_batch, sell_on, store


class ClosingBase(BaseCase):
    url = reverse("inventory:closing")

    def day_url(self, day):
        return f"{self.url}?data={day.isoformat()}"

    def add(self, weight, premium=False, receipt=True, url=None, **extra):
        data = {"azione": "pacco", "weight_kg": weight, **extra}
        if premium:
            data["premium"] = "on"
        if receipt:
            data["receipt"] = "on"
        return self.client.post(url or self.url, data)

    def close(self, total, url=None):
        return self.client.post(url or self.url, {"azione": "chiusura", "receipts_total": total})


class EntryTests(ClosingBase):
    def test_page_opens_with_the_form_and_the_price_rules_for_the_preview(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["store"], store("Milanofiori"))
        self.assertEqual(
            response.context["rules"],
            {"small_max_g": 200, "small_cents": 500, "rate_cents": 2500, "premium_cents": 3000},
        )
        self.assertContains(response, 'data-rate-cents="2500"')
        self.assertContains(response, "Scontrino emesso")
        self.assertContains(response, "Pacco premium")

    def test_receipt_is_ticked_by_default(self):
        self.assertTrue(self.client.get(self.url).context["entry_form"].fields["receipt"].initial)
        self.assertContains(self.client.get(self.url), 'name="receipt" id="id_receipt" checked')

    def test_adding_a_package_records_a_sale_with_the_computed_price(self):
        response = self.add("1,250")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        package = Package.objects.get()
        self.assertEqual(package.store, store("Milanofiori"))
        self.assertEqual(package.weight_kg, Decimal("1.250"))
        self.assertEqual(package.sold_price, Decimal("31.25"))
        self.assertTrue(package.is_sold and package.receipt and not package.premium)
        self.assertEqual((package.sold_by, package.created_by), (self.user, self.user))
        self.assertLess((timezone.now() - package.sold_at).total_seconds(), 60)
        self.assertFalse(package.has_photo)
        self.assertIsNone(package.batch)

    def test_prices_follow_the_rules(self):
        cases = [
            ("0,150", False, "5.00"),
            ("0,200", False, "5.00"),
            ("0,200", True, "5.00"),
            ("0,300", False, "7.50"),
            ("0,300", True, "9.00"),
            ("2", False, "50.00"),
            ("2", True, "60.00"),
        ]
        for weight, premium, expected in cases:
            with self.subTest(weight=weight, premium=premium):
                Package.objects.all().delete()
                self.add(weight, premium=premium)
                self.assertEqual(Package.objects.get().sold_price, Decimal(expected))

    def test_the_price_comes_from_the_current_rules(self):
        shop = ShopSettings.load()
        shop.price_per_kg = Decimal("20")
        shop.save()
        self.add("2")
        self.assertEqual(Package.objects.get().sold_price, Decimal("40.00"))

    def test_a_price_posted_by_the_manager_is_ignored(self):
        self.add("2", sold_price="1")
        self.assertEqual(Package.objects.get().sold_price, Decimal("50.00"))

    def test_without_receipt_is_recorded(self):
        self.add("1", receipt=False)
        self.assertFalse(Package.objects.get().receipt)

    def test_premium_is_recorded(self):
        self.add("1", premium=True)
        package = Package.objects.get()
        self.assertTrue(package.premium)
        self.assertEqual(package.sold_price, Decimal("30.00"))

    def test_comma_and_dot_decimals_are_accepted(self):
        for raw in ("1,5", "1.5"):
            self.assertEqual(self.add(raw).status_code, 302)
        self.assertEqual(Package.objects.filter(weight_kg=Decimal("1.500")).count(), 2)

    def test_invalid_weights_are_rejected(self):
        for raw in ("", "0", "-1", "abc", "1001", "2,3456"):
            with self.subTest(raw=raw):
                response = self.add(raw)
                self.assertEqual(response.status_code, 200)
                self.assertIn("weight_kg", response.context["entry_form"].errors)
        self.assertEqual(Package.objects.count(), 0)

    def test_entries_of_the_day_are_listed_newest_first_with_totals(self):
        self.add("1")
        self.add("0,5", premium=True, receipt=False)
        response = self.client.get(self.url)
        weights = [p.weight_kg for p in response.context["entries"]]
        self.assertEqual(weights, [Decimal("0.500"), Decimal("1.000")])
        self.assertEqual(response.context["count"], 2)
        self.assertEqual(response.context["revenue"], Decimal("40.00"))
        self.assertContains(response, "2 pacchi · 40,00 €")
        self.assertContains(response, "Premium")
        self.assertContains(response, "No scontrino")

    def test_only_this_centers_sales_of_this_day_are_listed(self):
        today = timezone.localdate()
        sell_on(today - timedelta(days=1), "3", "75")
        sell_on(today, "4", "100", store_name="Globo")
        self.add("1")
        response = self.client.get(self.url)
        self.assertEqual(response.context["count"], 1)
        self.assertEqual(response.context["revenue"], Decimal("25.00"))

    def test_owner_sees_the_yakuza_line_and_manager_does_not(self):
        self.add("1", receipt=False)
        self.assertNotContains(self.client.get(self.url), "Yakuza")
        self.as_owner()
        response = self.client.get(f"{self.url}?centro={store('Milanofiori').pk}")
        self.assertContains(response, "senza scontrino (Yakuza): 25,00 € (1)")

    def test_sale_of_a_registered_package_shows_in_the_closing_list_too(self):
        stock = create_package("2.350", make_batch("Lotto 1"))
        self.client.post(reverse("inventory:sell", args=[stock.pk]), {"receipt": "on"})
        response = self.client.get(self.url)
        self.assertEqual([p.pk for p in response.context["entries"]], [stock.pk])
        self.assertFalse(response.context["entries"][0].deletable)  # ha foto e lotto: si gestisce dal dettaglio


class DayTests(ClosingBase):
    def test_yesterday_can_be_filled_in(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        response = self.add("1", url=self.day_url(yesterday))
        self.assertRedirects(response, self.day_url(yesterday), fetch_redirect_response=False)
        package = Package.objects.get()
        self.assertEqual(timezone.localdate(package.sold_at), yesterday)
        self.assertEqual(timezone.localtime(package.sold_at).hour, 12)
        page = self.client.get(self.day_url(yesterday))
        self.assertEqual(page.context["count"], 1)
        self.assertFalse(page.context["is_today"])
        self.assertEqual(self.client.get(self.url).context["count"], 0)

    def test_today_is_the_default_and_bad_dates_fall_back_to_it(self):
        today = timezone.localdate()
        old = today - timedelta(days=2)
        bad = [old.isoformat(), (today + timedelta(days=1)).isoformat(), "abc", "2026-13-45", "", "20261012"]
        for raw in bad:
            with self.subTest(raw=raw):
                response = self.client.get(self.url, {"data": raw})
                self.assertEqual(response.context["day"], today)

    def test_a_manager_cannot_write_into_an_older_day(self):
        old = timezone.localdate() - timedelta(days=3)
        self.add("1", url=self.day_url(old))
        self.close("10", url=self.day_url(old))
        self.assertFalse(DayClose.objects.filter(date=old).exists())
        # i due inserimenti sono finiti su «oggi», non sul giorno vecchio
        self.assertEqual(timezone.localdate(Package.objects.get().sold_at), timezone.localdate())

    def test_an_owner_can_fill_in_any_past_day_but_not_the_future(self):
        self.as_owner()
        url = f"{self.url}?centro={store('Globo').pk}"
        old = timezone.localdate() - timedelta(days=10)
        self.add("1", url=f"{url}&data={old.isoformat()}")
        self.assertEqual(timezone.localdate(Package.objects.get().sold_at), old)
        future = timezone.localdate() + timedelta(days=1)
        self.assertEqual(self.client.get(f"{url}&data={future.isoformat()}").context["day"], timezone.localdate())

    def test_day_tabs_link_to_today_and_yesterday(self):
        response = self.client.get(self.url)
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertContains(response, f"?data={yesterday}")


class CloseTests(ClosingBase):
    def test_closing_the_day_saves_the_receipts_total(self):
        response = self.close("123,40")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        close = DayClose.objects.get()
        self.assertEqual((close.store, close.date, close.receipts_total), (store("Milanofiori"), timezone.localdate(), Decimal("123.40")))
        self.assertEqual(close.closed_by, self.user)
        page = self.client.get(self.url)
        self.assertContains(page, 'value="123,40"')
        self.assertContains(page, "Aggiorna la chiusura")

    def test_closing_again_updates_instead_of_duplicating(self):
        self.close("100")
        self.close("90,50")
        close = DayClose.objects.get()
        self.assertEqual(close.receipts_total, Decimal("90.50"))

    def test_zero_is_a_valid_total(self):
        self.assertEqual(self.close("0").status_code, 302)
        self.assertEqual(DayClose.objects.get().receipts_total, Decimal("0.00"))

    def test_invalid_totals_are_rejected(self):
        for raw in ("", "-1", "abc", "12345678901", "1,234"):
            with self.subTest(raw=raw):
                response = self.close(raw)
                self.assertEqual(response.status_code, 200)
                self.assertIn("receipts_total", response.context["close_form"].errors)
        self.assertEqual(DayClose.objects.count(), 0)

    def test_each_center_closes_its_own_day(self):
        close_day(timezone.localdate(), "50", "Globo")
        self.close("70")
        self.assertEqual(DayClose.objects.count(), 2)
        self.assertEqual(DayClose.objects.get(store=store("Globo")).receipts_total, Decimal("50.00"))

    def test_yesterday_can_be_closed_separately_from_today(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.close("10", url=self.day_url(yesterday))
        self.close("20")
        self.assertEqual(
            {c.date: c.receipts_total for c in DayClose.objects.all()},
            {yesterday: Decimal("10.00"), timezone.localdate(): Decimal("20.00")},
        )

    def test_unknown_action_does_nothing(self):
        response = self.client.post(self.url, {"azione": "boh", "weight_kg": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual((Package.objects.count(), DayClose.objects.count()), (0, 0))


class DeleteEntryTests(ClosingBase):
    def entry(self):
        self.add("1")
        return Package.objects.get()

    def test_delete_removes_an_entry_made_by_mistake(self):
        package = self.entry()
        response = self.client.post(reverse("inventory:closing_delete", args=[package.pk]))
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertFalse(Package.objects.exists())

    def test_delete_of_yesterday_returns_to_that_day(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.add("1", url=self.day_url(yesterday))
        response = self.client.post(reverse("inventory:closing_delete", args=[Package.objects.get().pk]))
        self.assertRedirects(response, self.day_url(yesterday), fetch_redirect_response=False)

    def test_delete_needs_post(self):
        package = self.entry()
        self.assertEqual(self.client.get(reverse("inventory:closing_delete", args=[package.pk])).status_code, 405)
        self.assertTrue(Package.objects.exists())

    def test_packages_with_a_photo_or_a_lot_are_not_deleted_from_here(self):
        with_photo = create_package("1", sold_price="25")
        with_lot = create_package("1", make_batch("L"), sold_price="25", photo=False)
        for package in (with_photo, with_lot):
            with self.subTest(package=package.pk):
                response = self.client.post(reverse("inventory:closing_delete", args=[package.pk]))
                self.assertEqual(response.status_code, 403)
                self.assertTrue(Package.objects.filter(pk=package.pk).exists())

    def test_unsold_packages_are_not_deletable_from_here(self):
        package = create_package("1", photo=False)
        self.assertEqual(self.client.post(reverse("inventory:closing_delete", args=[package.pk])).status_code, 404)

    def test_the_list_offers_delete_only_for_entries_made_at_closing(self):
        entry = self.entry()
        with_photo = create_package("2", sold_price="50")
        response = self.client.get(self.url)
        self.assertContains(response, reverse("inventory:closing_delete", args=[entry.pk]))
        self.assertNotContains(response, reverse("inventory:closing_delete", args=[with_photo.pk]))

    def test_the_owner_returns_to_the_same_center(self):
        self.as_owner()
        url = f"{self.url}?centro={store('Globo').pk}"
        self.add("1", url=url)
        package = Package.objects.get()
        response = self.client.post(reverse("inventory:closing_delete", args=[package.pk]))
        self.assertRedirects(response, url, fetch_redirect_response=False)
