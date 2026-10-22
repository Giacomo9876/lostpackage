"""Le tre medie (infrasettimanale, settimanale, fine settimana) calcolate dalle chiusure di giornata."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from .. import averages
from ..models import DayClose
from .helpers import close_day, store

D = Decimal

# Mercoledì 21 ottobre 2026: le settimane intere finite sono quelle fino a domenica 18.
TODAY = date(2026, 10, 21)
MON_5, TUE_6, WED_7, THU_8, FRI_9, SAT_10, SUN_11 = (date(2026, 10, d) for d in range(5, 12))
MON_12, SAT_17, SUN_18 = date(2026, 10, 12), date(2026, 10, 17), date(2026, 10, 18)


def row(day, receipts, yakuza="0", store_id=1):
    return (store_id, day, D(receipts), D(yakuza))


def period(key="90g", today=TODAY):
    return averages.get_period(key, today=today)


class BucketTests(SimpleTestCase):
    def test_weekdays_and_weekend_are_separate_and_all_days_is_both(self):
        rows = [row(MON_5, "100"), row(TUE_6, "200"), row(SAT_10, "400"), row(SUN_11, "600")]
        result = averages.compute(rows, period(), TODAY)
        self.assertEqual((result.weekdays.days, result.weekdays.avg_receipts), (2, D("150.00")))
        self.assertEqual((result.weekend.days, result.weekend.avg_receipts), (2, D("500.00")))
        self.assertEqual((result.all_days.days, result.all_days.avg_receipts), (4, D("325.00")))

    def test_friday_is_a_weekday_and_saturday_starts_the_weekend(self):
        result = averages.compute([row(FRI_9, "100"), row(SAT_10, "300")], period(), TODAY)
        self.assertEqual(result.weekdays.avg_receipts, D("100.00"))
        self.assertEqual(result.weekend.avg_receipts, D("300.00"))

    def test_yakuza_is_added_to_the_day(self):
        result = averages.compute([row(MON_5, "100", "40"), row(TUE_6, "200", "0")], period(), TODAY)
        b = result.weekdays
        self.assertEqual((b.avg_receipts, b.avg_yakuza, b.avg_total), (D("150.00"), D("20.00"), D("170.00")))
        self.assertEqual((b.receipts, b.yakuza, b.total), (D("300"), D("40"), D("340")))

    def test_days_without_a_close_are_not_counted_as_zero(self):
        result = averages.compute([row(MON_5, "100")], period(), TODAY)  # martedì–venerdì: nessun dato
        self.assertEqual((result.weekdays.days, result.weekdays.avg_receipts), (1, D("100.00")))
        self.assertEqual((result.weekend.days, result.weekend.avg_receipts), (0, None))

    def test_a_close_of_zero_is_a_real_day(self):
        result = averages.compute([row(MON_5, "100"), row(TUE_6, "0")], period(), TODAY)
        self.assertEqual((result.weekdays.days, result.weekdays.avg_receipts), (2, D("50.00")))

    def test_averages_are_rounded_to_the_cent_half_up(self):
        three = averages.compute([row(MON_5, "3.33"), row(TUE_6, "3.33"), row(WED_7, "3.34")], period(), TODAY)
        self.assertEqual(three.weekdays.avg_receipts, D("3.33"))
        half = averages.compute([row(MON_5, "0.01"), row(TUE_6, "0.00")], period(), TODAY)
        self.assertEqual(half.weekdays.avg_receipts, D("0.01"))  # 0,005 → 0,01

    def test_two_centers_on_the_same_day_make_one_day(self):
        rows = [row(MON_5, "100", store_id=1), row(MON_5, "50", store_id=2)]
        result = averages.compute(rows, period(), TODAY)
        self.assertEqual((result.weekdays.days, result.weekdays.avg_receipts), (1, D("150.00")))

    def test_nothing_to_average(self):
        result = averages.compute([], period(), TODAY)
        self.assertFalse(result.has_data)
        self.assertEqual((result.weekdays.avg_total, result.weekend.avg_total, result.all_days.avg_total), (None, None, None))
        self.assertEqual((result.weeks, result.week_receipts, result.week_yakuza, result.week_total), (0, None, None, None))


class WeeklyTests(SimpleTestCase):
    def full_week(self, monday, daily="100", yakuza="10"):
        return [row(monday + timedelta(days=n), daily, yakuza) for n in range(7)]

    def test_a_full_week_gives_the_weekly_takings(self):
        result = averages.compute(self.full_week(MON_5), period(), TODAY)
        self.assertEqual(result.weeks, 1)
        self.assertEqual((result.week_receipts, result.week_yakuza, result.week_total), (D("700.00"), D("70.00"), D("770.00")))

    def test_the_weekly_average_is_over_the_full_weeks(self):
        rows = self.full_week(MON_5, "100") + self.full_week(MON_12, "200")
        result = averages.compute(rows, period(), TODAY)
        self.assertEqual(result.weeks, 2)
        self.assertEqual(result.week_receipts, D("1050.00"))  # (700 + 1400) / 2

    def test_a_week_with_some_missing_closes_still_counts_with_what_there_is(self):
        rows = [row(MON_5, "100"), row(SAT_10, "300")]
        self.assertEqual(averages.compute(rows, period(), TODAY).week_receipts, D("400.00"))

    def test_the_current_week_is_left_out(self):
        rows = self.full_week(MON_5) + [row(date(2026, 10, 19), "999"), row(date(2026, 10, 20), "999"), row(TODAY, "999")]
        result = averages.compute(rows, period(), TODAY)
        self.assertEqual((result.weeks, result.week_receipts), (1, D("700.00")))
        # …ma le giornate di questa settimana contano per le medie al giorno
        self.assertEqual(result.weekdays.days, 5 + 3)

    def test_a_week_ending_today_is_not_finished_yet(self):
        sunday = date(2026, 10, 25)
        rows = self.full_week(date(2026, 10, 19))
        self.assertEqual(averages.compute(rows, period(today=sunday), sunday).weeks, 0)
        self.assertEqual(averages.compute(rows, period(today=sunday), date(2026, 10, 26)).weeks, 1)

    def test_a_week_cut_by_the_start_of_the_period_is_left_out(self):
        # 14 giorni che finiscono mercoledì 21: iniziano giovedì 8, quindi la settimana 5–11 è tagliata.
        rows = self.full_week(MON_5) + self.full_week(MON_12)
        result = averages.compute(rows, period("14g"), TODAY)
        self.assertEqual((result.weeks, result.week_receipts), (1, D("700.00")))

    def test_the_weeks_of_forever_start_from_the_first_close(self):
        from_wednesday = [row(WED_7 + timedelta(days=n), "100") for n in range(12)]
        self.assertEqual(averages.compute(from_wednesday, period("sempre"), TODAY).weeks, 1)
        from_monday = [row(MON_5 + timedelta(days=n), "100") for n in range(14)]
        self.assertEqual(averages.compute(from_monday, period("sempre"), TODAY).weeks, 2)

    def test_no_full_week_no_weekly_figure(self):
        result = averages.compute([row(MON_5, "100"), row(TUE_6, "100")] + [row(MON_12, "5")] * 1, period("14g"), TODAY)
        self.assertEqual((result.weeks, result.week_receipts), (1, D("5.00")))  # solo la 12–18 è dentro e finita
        short = averages.compute([row(TODAY, "100")], period("14g"), TODAY)
        self.assertEqual((short.weeks, short.week_receipts), (0, None))


class PeriodsTests(SimpleTestCase):
    def test_the_periods_are_the_long_ones(self):
        self.assertEqual([label for _, label in averages.PERIODS], ["2 settimane", "1 mese", "2 mesi", "3 mesi", "Sempre"])

    def test_default_and_unknown_values(self):
        self.assertEqual(averages.get_period(None, today=TODAY).key, "90g")
        for raw in ("oggi", "3g", "x", "7g;drop", "", "mese"):
            with self.subTest(raw=raw):
                self.assertEqual(averages.get_period(raw, today=TODAY).key, "90g")
        self.assertEqual(averages.get_period("14g", today=TODAY).key, "14g")

    def test_every_period_is_a_valid_stats_period(self):
        for key, _ in averages.PERIODS:
            self.assertEqual(averages.get_period(key, today=TODAY).key, key)


class RowsFromTheDatabaseTests(TestCase):
    def test_rows_are_the_closes_of_the_period(self):
        close_day(TODAY - timedelta(days=3), "100.00")
        close_day(TODAY - timedelta(days=40), "999.00")  # fuori da «1 mese»
        DayClose.objects.filter(date=TODAY - timedelta(days=3)).update(yakuza_cash=D("12.50"))
        close_day(TODAY - timedelta(days=2), "50.00", "Globo")
        rows = averages.closing_rows(DayClose.objects.all(), period("30g"))
        self.assertEqual(
            sorted(rows, key=lambda r: r[1]),
            [
                (store("Milanofiori").pk, TODAY - timedelta(days=3), D("100.00"), D("12.50")),
                (store("Globo").pk, TODAY - timedelta(days=2), D("50.00"), D("0.00")),
            ],
        )

    def test_by_store_keeps_the_centers_apart(self):
        close_day(MON_5, "100.00")
        close_day(MON_5, "300.00", "Globo")
        close_day(SAT_10, "500.00", "Globo")
        rows = averages.closing_rows(DayClose.objects.all(), period())
        result = averages.compute_by_store(rows, period(), TODAY)
        milan, globo = result[store("Milanofiori").pk], result[store("Globo").pk]
        self.assertEqual((milan.all_days.days, milan.weekdays.avg_receipts), (1, D("100.00")))
        self.assertEqual((globo.all_days.days, globo.weekdays.avg_receipts, globo.weekend.avg_receipts), (2, D("300.00"), D("500.00")))
        self.assertNotIn(store("Tor Vergata").pk, result)

    def test_the_new_fields_start_empty_for_old_closes(self):
        close = close_day(MON_5, "100.00")
        close.refresh_from_db()
        self.assertEqual((close.yakuza_cash, close.detected_total, bool(close.photo)), (D("0.00"), None, False))
        self.assertEqual((close.day_total, close.reading, close.has_photo), (D("100.00"), "typed", False))

    def test_how_the_amount_was_obtained(self):
        close = close_day(MON_5, "100.00")
        cases = [(None, "typed"), (D("100.00"), "read"), (D("90.00"), "corrected")]
        for detected, expected in cases:
            with self.subTest(detected=detected):
                close.detected_total = detected
                self.assertEqual(close.reading, expected)

    def test_a_day_total_adds_the_yakuza_cash(self):
        close = close_day(MON_5, "100.00")
        close.yakuza_cash = D("35.50")
        self.assertEqual(close.day_total, D("135.50"))


class ChartTests(SimpleTestCase):
    """Le barre del grafico: la media più alta è lunga quanto la riga, le altre in proporzione."""

    def test_bars_are_proportional_to_the_highest_average(self):
        rows = [row(MON_5, "100"), row(TUE_6, "100"), row(SAT_10, "400")]
        chart = averages.chart_rows(averages.compute(rows, period(), TODAY))
        self.assertEqual([r.title for r in chart], ["Infrasettimanale", "Settimanale", "Fine settimana"])
        # lun–ven 100, tutti i giorni 200, sab–dom 400
        self.assertEqual([r.pct for r in chart], [25, 50, 100])
        self.assertEqual([r.yakuza_pct for r in chart], [0, 0, 0])

    def test_the_yakuza_part_of_a_bar(self):
        chart = averages.chart_rows(averages.compute([row(MON_5, "75", "25")], period(), TODAY))
        weekdays = chart[0]
        self.assertEqual((weekdays.pct, weekdays.yakuza_pct), (100, 25))

    def test_a_tiny_average_still_shows_and_a_missing_one_has_no_bar(self):
        chart = averages.chart_rows(averages.compute([row(MON_5, "1"), row(SAT_10, "10000")], period(), TODAY))
        self.assertEqual(chart[0].pct, 2)
        only_weekdays = averages.chart_rows(averages.compute([row(MON_5, "100")], period(), TODAY))
        self.assertEqual([r.pct for r in only_weekdays], [100, 100, 0])

    def test_no_data_no_bars(self):
        chart = averages.chart_rows(averages.compute([], period(), TODAY))
        self.assertEqual([(r.pct, r.yakuza_pct) for r in chart], [(0, 0)] * 3)

    def test_days_of_zero_give_no_bar_and_no_error(self):
        chart = averages.chart_rows(averages.compute([row(MON_5, "0")], period(), TODAY))
        self.assertEqual([r.pct for r in chart], [0, 0, 0])
