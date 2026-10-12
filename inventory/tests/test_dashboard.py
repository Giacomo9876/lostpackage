"""Numeri della dashboard: venduto, Yakuza (senza scontrino), scontrini in cassa e differenze."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import stats
from ..models import DayClose, Package, ShopSettings
from .helpers import BaseCase, close_day, create_package, sell_on, store

D = Decimal


def totals(period_key="sempre", today=None):
    period = stats.get_period(period_key, today=today)
    rows = stats.daily_rows(stats.sold_in(Package.objects.all(), period))
    closes = list(stats.closes_in(DayClose.objects.all(), period))
    return stats.totals_by_store(rows, closes, today or timezone.localdate()), rows, closes


class PeriodTests(TestCase):
    today = date(2026, 10, 12)

    def test_periods(self):
        cases = {
            "oggi": (self.today, self.today),
            "7g": (date(2026, 10, 6), self.today),
            "30g": (date(2026, 9, 13), self.today),
            "mese": (date(2026, 10, 1), self.today),
            "sempre": (None, self.today),
        }
        for key, (start, end) in cases.items():
            with self.subTest(key=key):
                period = stats.get_period(key, today=self.today)
                self.assertEqual((period.key, period.start, period.end), (key, start, end))

    def test_unknown_period_falls_back_to_the_default(self):
        for raw in (None, "", "x", "7G", "1;drop"):
            with self.subTest(raw=raw):
                self.assertEqual(stats.get_period(raw, today=self.today).key, stats.DEFAULT_PERIOD)
        self.assertEqual(stats.get_period("x", today=self.today, default="oggi").key, "oggi")

    def test_days_are_local_days_not_utc_days(self):
        # 28 marzo 23:30 e 29 marzo 00:10 (ora locale, a ridosso del cambio all'ora legale)
        # sono giorni diversi per il negozio; in UTC sarebbero lo stesso giorno.
        late = stats.start_of_day(date(2026, 3, 29)) - timedelta(minutes=30)
        early = stats.start_of_day(date(2026, 3, 29)) + timedelta(minutes=10)
        create_package("1", sold_price="25", sold_at=late, photo=False)
        create_package("1", sold_price="25", sold_at=early, photo=False)
        period = stats.Period("x", "x", date(2026, 3, 28), date(2026, 3, 29))
        rows = stats.daily_rows(stats.sold_in(Package.objects.all(), period))
        self.assertEqual([(r["day"], r["count"]) for r in rows], [(date(2026, 3, 28), 1), (date(2026, 3, 29), 1)])

    def test_period_bounds_are_inclusive_of_whole_days(self):
        first, last = date(2026, 5, 10), date(2026, 5, 12)
        inside = [
            stats.start_of_day(first),
            stats.start_of_day(last) + timedelta(hours=23, minutes=59),
        ]
        outside = [stats.start_of_day(first) - timedelta(seconds=1), stats.start_of_day(last + timedelta(days=1))]
        for moment in inside + outside:
            create_package("1", sold_price="25", sold_at=moment, photo=False)
        period = stats.Period("x", "x", first, last)
        found = stats.sold_in(Package.objects.all(), period).count()
        self.assertEqual(found, 2)


class TotalsTests(TestCase):
    """Due centri, tre giorni: casi con scontrino, senza scontrino, chiusure giuste, sbagliate e mancanti."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.d1, cls.d2 = cls.today - timedelta(days=3), cls.today - timedelta(days=2)
        # Milanofiori
        sell_on(cls.d1, "1", "25.00")
        sell_on(cls.d1, "0.4", "10.00", receipt=False)
        close_day(cls.d1, "20.00")  # i pacchi con scontrino fanno 25: differenza +5
        sell_on(cls.d2, "2", "50.00")  # nessuna chiusura
        sell_on(cls.today, "1.2", "30.00")  # oggi: non è ancora «mancante»
        # Globo
        sell_on(cls.d1, "1.6", "40.00", store_name="Globo")
        close_day(cls.d1, "45.00", "Globo")  # differenza -5

    def test_totals_per_center(self):
        per_store, _, _ = totals(today=self.today)
        m, g = per_store[store("Milanofiori").pk], per_store[store("Globo").pk]
        self.assertEqual((m.count, m.revenue, m.no_receipt, m.no_receipt_count), (4, D("115.00"), D("10.00"), 1))
        self.assertEqual((m.with_receipt, m.with_receipt_count), (D("105.00"), 3))
        self.assertEqual((m.declared, m.recorded_closed, m.difference), (D("20.00"), D("25.00"), D("5.00")))
        self.assertEqual((m.closed_days, m.missing_closes), (1, 1))
        self.assertEqual((g.revenue, g.no_receipt, g.difference, g.missing_closes), (D("40.00"), D("0"), D("-5.00"), 0))
        self.assertEqual(m.weight, D("4.600"))

    def test_yakuza_percentage(self):
        per_store, _, _ = totals(today=self.today)
        self.assertEqual(per_store[store("Milanofiori").pk].no_receipt_pct, 9)  # 10 su 115
        self.assertEqual(stats.Totals().no_receipt_pct, 0)  # niente vendite: nessuna divisione per zero

    def test_grand_total_adds_the_centers_up(self):
        per_store, _, _ = totals(today=self.today)
        total = stats.sum_totals(per_store)
        self.assertEqual((total.count, total.revenue, total.no_receipt), (5, D("155.00"), D("10.00")))
        self.assertEqual((total.declared, total.recorded_closed), (D("65.00"), D("65.00")))
        self.assertEqual(total.missing_closes, 1)

    def test_closes_only_count_inside_the_period(self):
        per_store, _, _ = totals("oggi", today=self.today)
        self.assertEqual(stats.sum_totals(per_store).closed_days, 0)
        self.assertEqual(stats.sum_totals(per_store).revenue, D("30.00"))

    def test_today_is_never_a_missing_close(self):
        per_store, _, _ = totals("oggi", today=self.today)
        self.assertEqual(stats.sum_totals(per_store).missing_closes, 0)

    def test_a_close_without_sales_counts_as_pure_difference(self):
        close_day(self.today - timedelta(days=1), "12.00", "Tor Vergata")
        per_store, _, _ = totals(today=self.today)
        t = per_store[store("Tor Vergata").pk]
        self.assertEqual((t.count, t.declared, t.difference), (0, D("12.00"), D("-12.00")))

    def test_empty_period_is_all_zeros(self):
        per_store, _, _ = totals("30g", today=date(2020, 1, 1))
        total = stats.sum_totals(per_store)
        self.assertEqual((total.count, total.revenue, total.no_receipt, total.declared), (0, D("0"), D("0"), D("0")))

    def test_chart_has_one_bar_per_day_and_marks_the_yakuza_share(self):
        _, rows, _ = totals(today=self.today)
        bars = stats.chart_days(rows, stats.get_period("7g", today=self.today))
        self.assertEqual(len(bars), 7)
        by_day = {bar["day"]: bar for bar in bars}
        d1 = by_day[self.d1]
        self.assertEqual((d1["revenue"], d1["no_receipt"]), (D("75.00"), D("10.00")))
        self.assertEqual(d1["no_receipt_share"], 13)  # 10 su 75
        self.assertEqual(max(bar["height"] for bar in bars), 100)
        self.assertEqual(by_day[self.today - timedelta(days=1)]["height"], 0)  # giorno senza vendite

    def test_chart_is_capped_and_empty_without_sales(self):
        _, rows, _ = totals(today=self.today)
        self.assertEqual(len(stats.chart_days(rows, stats.get_period("sempre", today=self.today))), stats.CHART_DAYS)
        self.assertEqual(len(stats.chart_days(rows, stats.get_period("oggi", today=self.today))), 1)
        empty = stats.chart_days([], stats.get_period("7g", today=self.today))
        self.assertTrue(all(bar["height"] == 0 and bar["revenue"] == 0 for bar in empty))

    def test_day_list_is_newest_first_and_includes_close_only_days(self):
        close_day(self.today - timedelta(days=1), "7.00")
        mid = store("Milanofiori").pk
        _, rows, closes = totals(today=self.today)
        rows = [r for r in rows if r["store_id"] == mid]
        closes = [c for c in closes if c.store_id == mid]
        items = stats.day_list(rows, closes)
        self.assertEqual([i["day"] for i in items], [self.today, self.today - timedelta(days=1), self.d2, self.d1])
        quiet = items[1]
        self.assertEqual((quiet["count"], quiet["revenue"], quiet["closed"], quiet["difference"]), (0, D("0"), True, D("-7.00")))
        first = items[3]
        self.assertEqual((first["revenue"], first["no_receipt"], first["declared"], first["difference"]), (D("35.00"), D("10.00"), D("20.00"), D("5.00")))
        self.assertFalse(items[0]["closed"])
        self.assertIsNone(items[0]["difference"])

    def test_stock_value_uses_the_price_rules(self):
        create_package("2.000", photo=False)
        create_package("0.100", photo=False)  # piccolo: 5 €
        create_package("1.000", photo=False, premium=True)  # 30 €
        create_package("5", photo=False, store_name="Globo")
        result = stats.stock_by_store(Package.objects.in_stock(), ShopSettings())
        m = result[store("Milanofiori").pk]
        self.assertEqual((m["count"], m["weight"], m["value"]), (3, D("3.100"), D("85.00")))
        self.assertEqual(result[store("Globo").pk]["value"], D("125.00"))


class OwnerDashboardTests(BaseCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.today = timezone.localdate()
        sell_on(cls.today, "1", "25.00")
        sell_on(cls.today, "1.6", "40.00", receipt=False)
        close_day(cls.today, "20.00")
        sell_on(cls.today, "2", "50.00", store_name="Globo")

    def setUp(self):
        self.as_owner()
        self.url = reverse("inventory:dashboard")

    def test_overview_shows_the_totals_and_the_three_centers(self):
        response = self.client.get(self.url, {"periodo": "oggi"})
        total = response.context["total"]
        self.assertEqual((total.revenue, total.no_receipt, total.count), (D("115.00"), D("40.00"), 3))
        self.assertEqual(len(response.context["cards"]), 3)
        self.assertContains(response, "Yakuza")
        self.assertContains(response, "115,00 €")
        self.assertContains(response, "40,00 €")

    def test_the_default_period_for_owners_is_a_week(self):
        self.assertEqual(self.client.get(self.url).context["period"].key, "7g")
        self.assertEqual(self.client.get(self.url, {"periodo": "boh"}).context["period"].key, "7g")

    def test_each_card_has_its_own_numbers_and_closing_state(self):
        cards = {c["store"].name: c for c in self.client.get(self.url, {"periodo": "oggi"}).context["cards"]}
        self.assertEqual(cards["Milanofiori"]["totals"].revenue, D("65.00"))
        self.assertEqual(cards["Globo"]["totals"].revenue, D("50.00"))
        self.assertEqual(cards["Tor Vergata"]["totals"].revenue, D("0"))
        self.assertTrue(cards["Milanofiori"]["closed_today"])
        self.assertFalse(cards["Globo"]["closed_today"])
        response = self.client.get(self.url, {"periodo": "oggi"})
        self.assertContains(response, "Oggi chiuso")
        self.assertContains(response, "Oggi non ancora chiuso")

    def test_difference_is_shown_with_its_sign(self):
        # Milanofiori: pacchi con scontrino 25, in cassa 20 → +5
        response = self.client.get(self.url, {"periodo": "oggi"})
        self.assertContains(response, "+5,00 €")
        close = DayClose.objects.get()
        close.receipts_total = D("30.00")
        close.save()
        self.assertContains(self.client.get(self.url, {"periodo": "oggi"}), "-5,00 €")

    def test_missing_closes_are_flagged(self):
        sell_on(self.today - timedelta(days=2), "1", "25.00", store_name="Tor Vergata")
        response = self.client.get(self.url, {"periodo": "7g"})
        self.assertEqual(response.context["total"].missing_closes, 1)
        self.assertContains(response, "senza chiusura")
        self.assertContains(response, "1 chiusura mancante")
        self.assertNotContains(self.client.get(self.url, {"periodo": "oggi"}), "senza chiusura")

    def test_stock_is_summarised(self):
        create_package("2.000", photo=False)
        response = self.client.get(self.url)
        self.assertEqual(response.context["stock_total"]["count"], 1)
        self.assertEqual(response.context["stock_total"]["value"], D("50.00"))

    def test_chart_is_drawn_except_for_today_only(self):
        self.assertContains(self.client.get(self.url, {"periodo": "7g"}), 'class="bars"')
        self.assertNotContains(self.client.get(self.url, {"periodo": "oggi"}), 'class="bars"')

    def test_center_page_has_days_and_closing_button(self):
        mid = store("Milanofiori")
        response = self.client.get(reverse("inventory:store", args=[mid.pk]), {"periodo": "7g"})
        self.assertEqual(response.context["totals"].revenue, D("65.00"))
        self.assertEqual(len(response.context["days"]), 1)
        self.assertContains(response, "Yakuza")
        self.assertContains(response, f"{reverse('inventory:closing')}?centro={mid.pk}")
        self.assertContains(response, "Rivedi la chiusura di oggi")

    def test_center_page_of_an_unclosed_center_invites_to_close(self):
        response = self.client.get(reverse("inventory:store", args=[store("Globo").pk]))
        self.assertContains(response, "non ancora chiusa")
        self.assertContains(response, "Inserisci i pacchi e chiudi la giornata")


class ManagerDashboardTests(BaseCase):
    def test_manager_home_is_the_center_page_for_today(self):
        sell_on(timezone.localdate(), "1", "25.00")
        sell_on(timezone.localdate(), "1", "25.00", store_name="Globo")  # di un altro centro
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.context["period"].key, "oggi")
        self.assertEqual(response.context["totals"].revenue, D("25.00"))
        self.assertEqual(response.context["today_count"], 1)
        self.assertContains(response, "non ancora chiusa")
        self.assertContains(response, reverse("inventory:closing"))
        self.assertNotContains(response, "?centro=")

    def test_manager_sees_the_closed_state_after_closing(self):
        close_day(timezone.localdate(), "10.00")
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertContains(response, "chiusa</strong>, scontrini battuti 10,00 €")

    def test_manager_can_switch_period_but_stays_in_their_center(self):
        sell_on(timezone.localdate() - timedelta(days=3), "1", "25.00")
        sell_on(timezone.localdate() - timedelta(days=3), "1", "25.00", store_name="Globo")
        response = self.client.get(reverse("inventory:dashboard"), {"periodo": "7g"})
        self.assertEqual(response.context["totals"].revenue, D("25.00"))
        self.assertEqual(len(response.context["days"]), 1)

    def test_navigation_differs_by_role(self):
        manager_page = self.client.get(reverse("inventory:dashboard")).content.decode()
        self.assertIn(reverse("inventory:closing"), manager_page)
        self.assertNotIn(reverse("inventory:price"), manager_page)
        self.as_owner()
        owner_page = self.client.get(reverse("inventory:dashboard")).content.decode()
        self.assertIn(reverse("inventory:price"), owner_page)
        self.assertNotIn('class="dock-add"', owner_page)

    def test_the_header_shows_who_you_are(self):
        self.assertContains(self.client.get(reverse("inventory:dashboard")), "Milanofiori</span>")
        self.as_owner()
        self.assertContains(self.client.get(reverse("inventory:dashboard")), "Titolare</span>")
