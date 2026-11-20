"""Incasso giornaliero di tutti i centri: nella pagina Scontrini, il titolare con «Tutti i centri» vede
ogni giorno con la somma delle chiusure dei centri, il dettaglio per centro e chi non ha ancora chiuso."""
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .. import averages
from ..models import DayClose, Profile, Store
from .helpers import TEMP_MEDIA, make_user, store

RECEIPTS = reverse("inventory:receipts")


def close(day, store_name, receipts, yakuza="0"):
    return DayClose.objects.create(store=store(store_name), date=day, receipts_total=D(receipts), cash_excess=D(yakuza))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DailyTotalsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = make_user("tom", Profile.Role.OWNER)
        cls.manager = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        cls.today = timezone.localdate()
        cls.yesterday = cls.today - timedelta(days=1)
        # Oggi due centri su tre, ieri tutti e tre.
        close(cls.today, "Globo", "300", "50")
        close(cls.today, "Milanofiori", "500")
        close(cls.yesterday, "Globo", "200")
        close(cls.yesterday, "Milanofiori", "100", "10")
        close(cls.yesterday, "Tor Vergata", "400")

    def page(self, **params):
        return self.client.get(RECEIPTS, params)

    def test_each_day_adds_up_every_center(self):
        self.client.force_login(self.owner)
        groups = self.page().context["day_groups"]
        self.assertEqual([group.date for group in groups], [self.today, self.yesterday])
        today, yesterday = groups
        self.assertEqual((today.receipts, today.yakuza, today.total), (D("800.00"), D("50.00"), D("850.00")))
        self.assertEqual((yesterday.receipts, yesterday.yakuza, yesterday.total), (D("700.00"), D("10.00"), D("710.00")))
        self.assertEqual([c.store.name for c in today.closes], ["Globo", "Milanofiori"])
        self.assertEqual(today.total, sum(c.day_total for c in today.closes))

    def test_the_page_shows_totals_details_and_who_is_missing(self):
        self.client.force_login(self.owner)
        response = self.page()
        self.assertContains(response, "Incasso giornaliero · tutti i centri")
        self.assertContains(response, "850,00 €")
        self.assertContains(response, "scontrini 800,00 € + Yakuza 50,00 €")
        self.assertContains(response, "2 centri su 3")
        self.assertContains(response, "Manca: Tor Vergata")
        self.assertContains(response, "3 centri su 3")
        # sotto il totale, ogni centro con la sua chiusura e il link per correggerla
        self.assertContains(response, "350,00 €")  # Globo oggi: 300 + 50 di Yakuza
        self.assertContains(response, f"centro={store('Globo').pk}")
        self.assertNotContains(response, "<h2>Giornate")

    def test_missing_centers_are_listed_by_name(self):
        Store.objects.create(name="Montebello", city="Montebello della Battaglia")
        self.client.force_login(self.owner)
        response = self.page()
        self.assertContains(response, "Mancano: Montebello, Tor Vergata")
        self.assertContains(response, "3 centri su 4")

    def test_a_single_center_keeps_the_usual_list(self):
        self.client.force_login(self.owner)
        response = self.page(centro=store("Globo").pk)
        self.assertIsNone(response.context["day_groups"])
        self.assertContains(response, "<h2>Giornate")
        self.assertEqual({c.store.name for c in response.context["closes"]}, {"Globo"})

    def test_managers_never_get_the_total_of_all_centers(self):
        self.client.force_login(self.manager)
        for params in ({}, {"centro": store("Globo").pk}):
            with self.subTest(params=params):
                response = self.page(**params)
                self.assertIsNone(response.context["day_groups"])
                self.assertNotContains(response, "tutti i centri")
                self.assertNotContains(response, "850,00")
                self.assertEqual({c.store.name for c in response.context["closes"]}, {"Milanofiori"})

    def test_the_period_is_respected(self):
        close(self.today - timedelta(days=40), "Globo", "999")
        self.client.force_login(self.owner)
        self.assertEqual(len(self.page(periodo="14g").context["day_groups"]), 2)
        self.assertEqual(len(self.page(periodo="sempre").context["day_groups"]), 3)

    def test_the_list_stops_at_the_limit_without_cutting_a_day(self):
        close(self.today - timedelta(days=2), "Globo", "10")
        self.client.force_login(self.owner)
        with mock.patch.object(averages, "DAY_LIMIT", 2):
            response = self.page()
        groups = response.context["day_groups"]
        self.assertEqual([len(group.closes) for group in groups], [2, 3])  # i giorni mostrati sono interi
        self.assertTrue(response.context["more_days"])
        self.assertContains(response, "Sono mostrati gli ultimi 2 giorni")

    def test_no_closes_no_days(self):
        DayClose.objects.all().delete()
        self.client.force_login(self.owner)
        response = self.page()
        self.assertEqual(response.context["day_groups"], [])
        self.assertContains(response, "Nessuna giornata chiusa nel periodo scelto.")

    def test_the_queries_do_not_grow_with_the_days(self):
        self.client.force_login(self.owner)
        self.page()  # riscalda sessione e cache dei template
        with CaptureQueriesContext(connection) as few:
            self.page()
        for offset in range(2, 40):
            close(self.today - timedelta(days=offset), "Tor Vergata", "100")
        with CaptureQueriesContext(connection) as many:
            self.page()
        self.assertEqual(len(many), len(few))
