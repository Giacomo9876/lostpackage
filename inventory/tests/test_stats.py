"""Andamento delle vendite: periodi, confronto col periodo prima, indicatori, ripartizione dei pagamenti, grafico."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .. import stats
from ..models import DayClose, Package
from .helpers import BaseCase, close_day, sell_on, store

D = Decimal


class CompareWithTests(SimpleTestCase):
    today = date(2026, 10, 15)

    def compare(self, key, today=None):
        return stats.compare_with(stats.get_period(key, today=today or self.today))

    def test_last_n_days_are_compared_with_the_n_days_just_before(self):
        for key, n in (("3g", 3), ("7g", 7), ("14g", 14), ("30g", 30), ("60g", 60), ("90g", 90)):
            with self.subTest(key=key):
                period = stats.get_period(key, today=self.today)
                before, text = stats.compare_with(period)
                self.assertEqual(before.end, period.start - timedelta(days=1))
                self.assertEqual(before.days, n)
                self.assertEqual(text, f"rispetto ai {n} giorni precedenti")

    def test_the_two_periods_never_overlap_and_touch(self):
        period = stats.get_period("14g", today=self.today)
        before, _ = stats.compare_with(period)
        self.assertEqual(before.end + timedelta(days=1), period.start)

    def test_today_and_forever_have_nothing_to_compare_with(self):
        self.assertIsNone(self.compare("oggi"))
        self.assertIsNone(self.compare("sempre"))

    def test_this_month_is_compared_with_the_same_days_of_last_month(self):
        before, text = self.compare("mese")
        self.assertEqual((before.start, before.end), (date(2026, 9, 1), date(2026, 9, 15)))
        self.assertEqual(text, "rispetto allo stesso periodo del mese scorso")

    def test_this_month_on_the_31st_is_capped_to_a_shorter_previous_month(self):
        before, _ = self.compare("mese", today=date(2026, 3, 31))
        self.assertEqual((before.start, before.end), (date(2026, 2, 1), date(2026, 2, 28)))
        before, _ = self.compare("mese", today=date(2028, 3, 31))  # bisestile
        self.assertEqual(before.end, date(2028, 2, 29))

    def test_january_is_compared_with_december_of_the_year_before(self):
        before, _ = self.compare("mese", today=date(2026, 1, 10))
        self.assertEqual((before.start, before.end), (date(2025, 12, 1), date(2025, 12, 10)))

    def test_first_day_of_the_month(self):
        before, _ = self.compare("mese", today=date(2026, 10, 1))
        self.assertEqual((before.start, before.end), (date(2026, 9, 1), date(2026, 9, 1)))


class ChangePctTests(SimpleTestCase):
    def test_changes(self):
        cases = [
            (D("120"), D("100"), 20),
            (D("80"), D("100"), -20),
            (D("100"), D("100"), 0),
            (D("0"), D("100"), -100),
            (D("100.50"), D("100"), 1),  # arrotondato a intero
            (D("200"), D("100"), 100),
            (D("1000"), D("100"), 900),
            (3, 2, 50),
        ]
        for now, before, expected in cases:
            with self.subTest(now=now, before=before):
                self.assertEqual(stats.change_pct(now, before), expected)

    def test_halves_round_away_from_zero(self):
        self.assertEqual(stats.change_pct(D("100.5"), D("100")), 1)
        self.assertEqual(stats.change_pct(D("99.5"), D("100")), -1)

    def test_no_percentage_without_a_term_of_comparison(self):
        for before in (0, D("0"), D("0.00"), None):
            with self.subTest(before=before):
                self.assertIsNone(stats.change_pct(D("50"), before))


class TotalsPropertiesTests(SimpleTestCase):
    def test_averages(self):
        t = stats.Totals(count=4, weight=D("5.000"), revenue=D("125.00"), premium_count=1)
        self.assertEqual(t.avg_price, D("31.25"))
        self.assertEqual(t.avg_weight, D("1.25"))
        self.assertEqual(t.price_per_kg, D("25.00"))
        self.assertEqual(t.premium_pct, 25)

    def test_averages_are_rounded_half_up(self):
        t = stats.Totals(count=3, weight=D("1.000"), revenue=D("10.00"))
        self.assertEqual(t.avg_price, D("3.33"))
        t = stats.Totals(count=8, weight=D("1.000"), revenue=D("0.05"))
        self.assertEqual(t.avg_price, D("0.01"))  # 0,00625 → 0,01

    def test_nothing_sold_means_none_not_a_division_by_zero(self):
        t = stats.Totals()
        self.assertEqual((t.avg_price, t.avg_weight, t.price_per_kg), (None, None, None))
        self.assertEqual((t.premium_pct, t.yakuza_pct), (0, 0))
        self.assertEqual([seg["pct"] for seg in t.mix], [0, 0, 0, 0])

    def test_receipt_and_yakuza_split(self):
        t = stats.Totals(count=5, revenue=D("100.00"), cash=D("40.00"), card=D("30.00"), other=D("10.00"), yakuza=D("20.00"), yakuza_count=2)
        self.assertEqual((t.with_receipt, t.with_receipt_count, t.yakuza_pct), (D("80.00"), 3, 20))

    def test_mix_lists_every_method_in_a_stable_order(self):
        t = stats.Totals(revenue=D("100.00"), cash=D("40.00"), card=D("30.00"), other=D("10.00"), yakuza=D("20.00"))
        self.assertEqual([seg["key"] for seg in t.mix], ["cash", "card", "other", "yakuza"])
        self.assertEqual([seg["label"] for seg in t.mix], ["Contanti", "Carta", "Altro", "Yakuza"])
        self.assertEqual([seg["pct"] for seg in t.mix], [40, 30, 10, 20])
        self.assertEqual([seg["weight"] for seg in t.mix], [4000, 3000, 1000, 2000])
        self.assertEqual(sum(seg["amount"] for seg in t.mix), t.revenue)

    def test_adding_totals_adds_every_field(self):
        a = stats.Totals(count=1, revenue=D("10"), cash=D("10"), weight=D("1.5"))
        b = stats.Totals(count=2, revenue=D("30"), yakuza=D("30"), yakuza_count=2, weight=D("0.5"))
        a += b
        self.assertEqual((a.count, a.revenue, a.cash, a.yakuza, a.yakuza_count, a.weight), (3, D("40"), D("10"), D("30"), 2, D("2.0")))


class PaymentAggregationTests(TestCase):
    """Le vendite si dividono per metodo di pagamento, per centro e per giorno."""

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        sell_on(cls.today, "1", "25.00", payment="cash")
        sell_on(cls.today, "1", "25.00", payment="card")
        sell_on(cls.today, "0.4", "10.00", payment="other")
        sell_on(cls.today, "2", "50.00", payment="yakuza")
        sell_on(cls.today, "1", "25.00", payment="card", store_name="Globo")
        sell_on(cls.today - timedelta(days=1), "1", "25.00", payment="cash")

    def totals(self, key="7g"):
        period = stats.get_period(key, today=self.today)
        rows = stats.daily_rows(stats.sold_in(Package.objects.all(), period))
        return stats.totals_by_store(rows, [], self.today), rows

    def test_each_method_is_summed_separately(self):
        per_store, _ = self.totals("oggi")
        m = per_store[store("Milanofiori").pk]
        self.assertEqual((m.cash, m.card, m.other, m.yakuza), (D("25.00"), D("25.00"), D("10.00"), D("50.00")))
        self.assertEqual(m.cash + m.card + m.other + m.yakuza, m.revenue)
        self.assertEqual(m.yakuza_count, 1)

    def test_the_methods_always_add_up_to_the_revenue(self):
        per_store, _ = self.totals("7g")
        total = stats.sum_totals(per_store)
        self.assertEqual(total.cash + total.card + total.other + total.yakuza, total.revenue)
        self.assertEqual(total.revenue, D("160.00"))

    def test_centers_do_not_mix(self):
        per_store, _ = self.totals("oggi")
        g = per_store[store("Globo").pk]
        self.assertEqual((g.card, g.cash, g.yakuza), (D("25.00"), D("0"), D("0")))

    def test_period_filters_the_methods_too(self):
        per_store, _ = self.totals("oggi")
        self.assertEqual(stats.sum_totals(per_store).cash, D("25.00"))  # l'altro contante è di ieri

    def test_only_yakuza_is_out_of_the_receipt_difference(self):
        # Il confronto con gli scontrini battuti non conta Yakuza, ma conta contanti, carta e altro.
        close_day(self.today, "60.00")  # oggi: 25 contanti + 25 carta + 10 altro con scontrino; 50 Yakuza no
        period = stats.get_period("oggi", today=self.today)
        rows = stats.daily_rows(stats.sold_in(Package.objects.all(), period))
        closes = list(stats.closes_in(DayClose.objects.all(), period))
        per_store = stats.totals_by_store(rows, closes, self.today)
        m = per_store[store("Milanofiori").pk]
        self.assertEqual((m.recorded_closed, m.declared, m.difference), (D("60.00"), D("60.00"), D("0.00")))

    def test_legacy_sale_without_a_list_price_is_still_counted(self):
        sell_on(self.today, "1", "20.00")  # nessun listino registrato
        per_store, _ = self.totals("oggi")
        self.assertEqual(per_store[store("Milanofiori").pk].count, 5)


class InsightsTests(TestCase):
    today = date(2026, 10, 15)

    def build(self, key, sales, today=None):
        today = today or self.today
        for offset, price, *rest in sales:
            sell_on(today - timedelta(days=offset), "1", price, *rest)
        period = stats.get_period(key, today=today)
        rows = stats.daily_rows(stats.sold_in(Package.objects.all(), period))
        totals = stats.sum_totals(stats.totals_by_store(rows, [], today))
        return stats.insights(rows, totals, period, today), totals, rows, period

    def test_daily_average_counts_days_without_sales_too(self):
        insights, totals, *_ = self.build("7g", [(0, "70.00")])
        self.assertEqual(insights.daily_avg, D("10.00"))  # 70 € in 7 giorni, non in 1
        self.assertEqual(insights.active_days, 1)

    def test_best_day_is_the_one_with_most_revenue(self):
        insights, *_ = self.build("14g", [(0, "30.00"), (1, "30.00"), (5, "90.00"), (5, "10.00"), (9, "50.00")])
        self.assertEqual(insights.best_day, self.today - timedelta(days=5))
        self.assertEqual(insights.best_revenue, D("100.00"))
        self.assertEqual(insights.active_days, 4)

    def test_on_a_tie_the_most_recent_day_wins(self):
        insights, *_ = self.build("7g", [(0, "40.00"), (3, "40.00")])
        self.assertEqual(insights.best_day, self.today)

    def test_best_day_sums_the_centers(self):
        insights, *_ = self.build("7g", [(2, "30.00"), (2, "30.00", "Globo"), (0, "50.00")])
        self.assertEqual((insights.best_day, insights.best_revenue), (self.today - timedelta(days=2), D("60.00")))

    def test_forever_averages_from_the_first_sale(self):
        insights, *_ = self.build("sempre", [(9, "50.00"), (0, "50.00")])
        self.assertEqual(insights.daily_avg, D("10.00"))  # 100 € in 10 giorni

    def test_this_month_averages_over_the_days_so_far(self):
        insights, *_ = self.build("mese", [(0, "150.00")])
        self.assertEqual(insights.daily_avg, D("10.00"))  # 15 giorni da inizio mese

    def test_no_sales_at_all(self):
        insights, totals, *_ = self.build("7g", [])
        self.assertEqual((insights.best_day, insights.active_days, insights.best_revenue), (None, 0, D("0")))
        self.assertEqual(insights.daily_avg, D("0.00"))
        insights, *_ = self.build("sempre", [])
        self.assertIsNone(insights.daily_avg)  # nessun giorno da cui partire: niente divisione per zero


class ChartTests(SimpleTestCase):
    today = date(2026, 10, 15)

    def bars(self, key, rows=()):
        return stats.chart_days(list(rows), stats.get_period(key, today=self.today))

    def test_one_bar_per_day_of_each_period(self):
        for key, n in (("oggi", 1), ("3g", 3), ("7g", 7), ("14g", 14), ("30g", 30), ("60g", 60), ("90g", 90)):
            with self.subTest(key=key):
                self.assertEqual(len(self.bars(key)), n)

    def test_bars_are_in_chronological_order_ending_today(self):
        bars = self.bars("14g")
        days = [bar["day"] for bar in bars]
        self.assertEqual(days, sorted(days))
        self.assertEqual(days[-1], self.today)
        self.assertEqual(days[0], self.today - timedelta(days=13))

    def test_the_first_and_the_last_bar_always_have_their_date(self):
        for key in ("3g", "7g", "14g", "30g", "60g", "90g"):
            with self.subTest(key=key):
                bars = self.bars(key)
                self.assertTrue(bars[0]["label"] and bars[-1]["label"])

    def test_dates_are_thinned_out_on_long_periods(self):
        counts = {key: sum(bar["label"] for bar in self.bars(key)) for key in ("7g", "14g", "30g", "90g")}
        self.assertEqual(counts["7g"], 7)  # ogni giorno
        self.assertLessEqual(counts["14g"], 8)
        self.assertLessEqual(counts["30g"], 8)
        self.assertLessEqual(counts["90g"], 11)

    def test_labels_are_never_crowded_together(self):
        for key in ("14g", "30g", "60g", "90g"):
            with self.subTest(key=key):
                positions = [i for i, bar in enumerate(self.bars(key)) if bar["label"]]
                gaps = [b - a for a, b in zip(positions, positions[1:])]
                self.assertGreaterEqual(min(gaps), 2)

    def test_bars_are_scaled_to_the_best_day(self):
        rows = [
            {"day": self.today, "revenue": D("100.00"), "yakuza": D("25.00")},
            {"day": self.today - timedelta(days=1), "revenue": D("50.00"), "yakuza": D("0")},
        ]
        bars = self.bars("3g", rows)
        self.assertEqual([bar["height"] for bar in bars], [0, 50, 100])
        self.assertEqual([bar["yakuza_share"] for bar in bars], [0, 0, 25])

    def test_rows_of_several_centers_in_the_same_day_are_added(self):
        rows = [
            {"day": self.today, "revenue": D("30.00"), "yakuza": D("0")},
            {"day": self.today, "revenue": D("70.00"), "yakuza": D("10.00")},
        ]
        last = self.bars("3g", rows)[-1]
        self.assertEqual((last["revenue"], last["yakuza"]), (D("100.00"), D("10.00")))

    def test_forever_is_capped_to_the_last_90_days(self):
        self.assertEqual(len(self.bars("sempre")), stats.CHART_DAYS)

    def test_no_sales_gives_flat_bars(self):
        self.assertTrue(all(bar["height"] == 0 for bar in self.bars("30g")))


class TrendPagesTests(BaseCase):
    """Lo si vede davvero nelle pagine: periodi, andamento, e cosa resta riservato ai titolari."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.today = timezone.localdate()
        # Ultimi 7 giorni: 200 €, di cui 50 Yakuza. I 7 giorni prima: 100 €.
        for offset in (0, 1, 2, 3):
            sell_on(cls.today - timedelta(days=offset), "2", "50.00", payment="yakuza" if offset == 0 else "cash")
        for offset in (8, 9):
            sell_on(cls.today - timedelta(days=offset), "2", "50.00", payment="card")

    def center(self, **params):
        return self.client.get(reverse("inventory:store", args=[store("Milanofiori").pk]), params)

    def test_every_requested_period_is_a_chip(self):
        self.as_owner()
        for url in (reverse("inventory:dashboard"), reverse("inventory:store", args=[store("Milanofiori").pk])):
            response = self.client.get(url)
            for key, label in stats.PERIODS:
                with self.subTest(url=url, key=key):
                    self.assertContains(response, f">{label}</a>")
            for label in ("3 giorni", "7 giorni", "2 settimane", "1 mese", "2 mesi", "3 mesi"):
                self.assertContains(response, label)

    def test_every_period_opens_for_owners_and_managers(self):
        for who in ("manager", "owner"):
            if who == "owner":
                self.as_owner()
            for key, _ in stats.PERIODS:
                with self.subTest(who=who, key=key):
                    self.assertEqual(self.center(periodo=key).context["period"].key, key)
                    self.assertEqual(self.client.get(reverse("inventory:dashboard"), {"periodo": key}).status_code, 200)

    def test_each_period_selects_its_own_sales(self):
        expected = {"oggi": D("50.00"), "3g": D("150.00"), "7g": D("200.00"), "14g": D("300.00"), "90g": D("300.00")}
        for key, revenue in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.center(periodo=key).context["totals"].revenue, revenue)

    def test_the_trend_compares_with_the_previous_period(self):
        response = self.center(periodo="7g")
        self.assertEqual(response.context["revenue_change"], 100)  # 200 contro 100
        self.assertEqual(response.context["count_change"], 100)
        self.assertContains(response, "▲ 100%")
        self.assertContains(response, "rispetto ai 7 giorni precedenti")

    def test_a_decline_is_shown_as_a_decline(self):
        self.assertEqual(self.center(periodo="3g").context["revenue_change"], 200)  # 150 € contro 50 €
        Package.objects.filter(sold_at__date__lt=self.today - timedelta(days=5)).update(sold_price=D("500.00"))
        response = self.center(periodo="7g")  # 200 € contro 1000 €
        self.assertLess(response.context["revenue_change"], 0)
        self.assertContains(response, "▼")

    def test_no_comparison_for_today_and_forever(self):
        for key in ("oggi", "sempre"):
            with self.subTest(key=key):
                response = self.center(periodo=key)
                self.assertIsNone(response.context["compare_label"])
                self.assertNotContains(response, "rispetto ai")
                self.assertNotContains(response, "▲")

    def test_no_percentage_when_there_was_nothing_before(self):
        response = self.center(periodo="90g")  # i 90 giorni prima sono vuoti
        self.assertIsNone(response.context["revenue_change"])
        self.assertContains(response, "nessun dato prima")

    def test_the_metrics_are_there(self):
        response = self.center(periodo="7g")
        self.assertContains(response, "Andamento · 7 giorni")
        for label in ("Media al giorno", "Giorno migliore", "Per pacco", "Prezzo medio al kg", "Pacchi premium", "Giorni con vendite"):
            self.assertContains(response, label)
        self.assertEqual(response.context["insights"].daily_avg, D("28.57"))  # 200 / 7
        self.assertContains(response, "28,57 €")
        self.assertContains(response, "4 su 7")

    def test_a_period_without_sales_says_so_instead_of_showing_zeros(self):
        self.assertContains(self.center(periodo="oggi"), "Giorno migliore")  # oggi c'è una vendita
        Package.objects.filter(sold_at__date=self.today).delete()
        response = self.center(periodo="oggi")
        self.assertContains(response, "Nessuna vendita nel periodo scelto.")
        self.assertNotContains(response, "Giorno migliore")

    def test_the_chart_covers_the_whole_period(self):
        self.assertEqual(len(self.center(periodo="90g").context["bars"]), 90)
        self.assertEqual(len(self.center(periodo="14g").context["bars"]), 14)
        self.assertEqual(self.center(periodo="oggi").context["bars"], [])

    def test_the_payment_mix_is_for_owners_only(self):
        self.as_owner()
        response = self.center(periodo="7g")
        self.assertContains(response, "Pagamenti")
        self.assertContains(response, "mix-legend")
        mix = {seg["key"]: seg["amount"] for seg in response.context["totals"].mix}
        self.assertEqual(mix, {"cash": D("150.00"), "card": D("0"), "other": D("0"), "yakuza": D("50.00")})

    def test_a_manager_sees_the_trend_but_no_payment_figures(self):
        response = self.center(periodo="7g")
        self.assertContains(response, "Andamento")
        self.assertContains(response, "▲ 100%")
        for hidden in ("Pagamenti", "mix-legend", "mix-seg", "bar-yak", "Sconti", "Differenza", "Scontrini battuti in cassa"):
            self.assertNotContains(response, hidden)
        self.assertNotContains(response, "swatch-yak")

    def test_a_manager_never_gets_the_overview_even_with_periods(self):
        response = self.client.get(reverse("inventory:dashboard"), {"periodo": "90g"})
        self.assertEqual(response.context["store"], store("Milanofiori"))
        self.assertNotContains(response, "Tutti i centri")

    def test_the_overview_shows_the_trend_per_center(self):
        self.as_owner()
        response = self.client.get(reverse("inventory:dashboard"), {"periodo": "7g"})
        cards = {c["store"].name: c for c in response.context["cards"]}
        self.assertEqual(cards["Milanofiori"]["change"], 100)
        self.assertIsNone(cards["Globo"]["change"])
        self.assertEqual(response.context["revenue_change"], 100)
        self.assertContains(response, "sul periodo precedente")

    def test_the_overview_has_the_chart_and_the_insights(self):
        self.as_owner()
        response = self.client.get(reverse("inventory:dashboard"), {"periodo": "30g"})
        self.assertContains(response, 'class="bars"')
        self.assertContains(response, "Andamento · 1 mese")
        self.assertEqual(len(response.context["bars"]), 30)

    def test_the_period_in_the_url_is_not_trusted(self):
        for raw in ("<script>", "7g;DROP", "../..", "1000g", "-3g", "%00"):
            with self.subTest(raw=raw):
                response = self.center(periodo=raw)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["period"].key, "oggi")  # il predefinito dei manager
                self.assertNotContains(response, raw)
