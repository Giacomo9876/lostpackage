"""Sezione Risorse: semaforo di pallet, sticker e scotch per centro e punto, visibile solo a chi deve."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from ..models import Franchise, Profile, SupplyItem, SupplyStatus
from .helpers import make_user, store

URL = reverse("inventory:supplies")
GREEN, YELLOW, RED = SupplyStatus.Level.GREEN, SupplyStatus.Level.YELLOW, SupplyStatus.Level.RED


def item(name):
    return SupplyItem.objects.get(name=name)


class SuppliesCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mario = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        cls.lucia = make_user("lucia", Profile.Role.MANAGER, "Globo")
        cls.tom = make_user("tom", Profile.Role.OWNER)
        cls.napoli = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        cls.anna = make_user("anna")
        Profile.objects.create(user=cls.anna, role=Profile.Role.FRANCHISE, franchise=cls.napoli)
        cls.pallet = item("Pallet / merce")
        cls.sticker30 = item("Sticker 30%")
        cls.scotch = item("Scotch")

    def setUp(self):
        self.client.force_login(self.mario)

    def status(self, it, level, place=None, **extra):
        place = place or store()
        key = "franchise" if isinstance(place, Franchise) else "store"
        return SupplyStatus.objects.create(item=it, level=level, **{key: place}, **extra)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]


class ItemsTests(SuppliesCase):
    def test_the_five_resources_exist_in_order(self):
        names = list(SupplyItem.objects.values_list("name", flat=True))
        self.assertEqual(names, ["Pallet / merce", "Sticker 30%", "Sticker premium", "Sticker 5-10 €", "Scotch"])
        self.assertEqual(list(SupplyItem.objects.filter(has_quantity=True).values_list("name", "unit")), [("Pallet / merce", "pallet")])

    def test_a_status_belongs_to_a_store_or_a_franchise_never_both_and_once(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SupplyStatus.objects.create(item=self.pallet, level=GREEN, store=store(), franchise=self.napoli)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SupplyStatus.objects.create(item=self.pallet, level=GREEN)
        self.status(self.pallet, GREEN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.status(self.pallet, RED)
        self.status(self.pallet, RED, place=self.napoli)  # lo stesso articolo in un punto va bene


class ManagerTests(SuppliesCase):
    def form_data(self, **values):
        data = {}
        for it in SupplyItem.objects.all():
            data[f"level_{it.pk}"] = ""
            data[f"note_{it.pk}"] = ""
            if it.has_quantity:
                data[f"quantity_{it.pk}"] = ""
        data.update(values)
        return data

    def test_the_form_has_a_light_for_every_resource_and_a_quantity_only_for_pallets(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["overview"])
        self.assertEqual(response.context["place"], store("Milanofiori"))
        for it in SupplyItem.objects.all():
            self.assertContains(response, f'name="level_{it.pk}"', count=3)
            self.assertContains(response, f'name="note_{it.pk}"')
        self.assertContains(response, f'name="quantity_{self.pallet.pk}"')
        self.assertNotContains(response, f'name="quantity_{self.scotch.pk}"')
        for label in ("OK", "In esaurimento", "Finito", "Quantità (pallet)"):
            self.assertContains(response, label)

    def test_saving_the_resources_of_the_own_center(self):
        data = self.form_data(**{
            f"level_{self.pallet.pk}": "yellow",
            f"quantity_{self.pallet.pk}": "3",
            f"note_{self.pallet.pk}": "  arriva giovedì ",
            f"level_{self.scotch.pk}": "red",
        })
        response = self.client.post(URL, data, follow=True)
        self.assertIn("Risorse aggiornate (2).", self.messages(response))
        pallet = SupplyStatus.objects.get(item=self.pallet)
        self.assertEqual((pallet.store, pallet.franchise, pallet.level, pallet.quantity, pallet.note, pallet.updated_by),
                         (store("Milanofiori"), None, YELLOW, 3, "arriva giovedì", self.mario))
        self.assertEqual(SupplyStatus.objects.get(item=self.scotch).level, RED)
        self.assertEqual(SupplyStatus.objects.count(), 2)  # le altre non erano indicate

    def test_the_page_shows_what_was_saved(self):
        self.status(self.pallet, YELLOW, quantity=4, note="pochi", updated_by=self.mario)
        response = self.client.get(URL)
        form = response.context["form"]
        self.assertEqual(form[f"level_{self.pallet.pk}"].value(), "yellow")
        self.assertEqual(form[f"quantity_{self.pallet.pk}"].value(), 4)
        self.assertEqual(form[f"note_{self.pallet.pk}"].value(), "pochi")
        self.assertContains(response, "Aggiornato da mario")
        self.assertContains(response, "Non ancora indicato.")

    def test_saving_the_same_values_changes_nothing(self):
        status = self.status(self.pallet, GREEN, quantity=5)
        before = status.updated_at
        response = self.client.post(URL, self.form_data(**{f"level_{self.pallet.pk}": "green", f"quantity_{self.pallet.pk}": "5"}), follow=True)
        self.assertIn("Nessuna modifica: le risorse erano già così.", self.messages(response))
        status.refresh_from_db()
        self.assertEqual(status.updated_at, before)

    def test_bad_values_are_refused(self):
        cases = [
            ({f"level_{self.pallet.pk}": "blue"}, f"level_{self.pallet.pk}"),
            ({f"level_{self.pallet.pk}": "green", f"quantity_{self.pallet.pk}": "-1"}, f"quantity_{self.pallet.pk}"),
            ({f"level_{self.pallet.pk}": "green", f"quantity_{self.pallet.pk}": "100001"}, f"quantity_{self.pallet.pk}"),
            ({f"level_{self.pallet.pk}": "green", f"quantity_{self.pallet.pk}": "tre"}, f"quantity_{self.pallet.pk}"),
            ({f"level_{self.scotch.pk}": "green", f"note_{self.scotch.pk}": "x" * 201}, f"note_{self.scotch.pk}"),
            ({f"note_{self.scotch.pk}": "ne serve"}, f"level_{self.scotch.pk}"),  # nota senza colore
            ({f"quantity_{self.pallet.pk}": "2"}, f"level_{self.pallet.pk}"),  # quantità senza colore
        ]
        for values, field in cases:
            with self.subTest(values=values):
                response = self.client.post(URL, self.form_data(**values))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
        self.assertFalse(SupplyStatus.objects.exists())

    def test_the_center_cannot_be_chosen_from_the_request(self):
        data = self.form_data(**{f"level_{self.scotch.pk}": "red", "store": store("Globo").pk, "franchise": self.napoli.pk})
        self.client.post(f"{URL}?centro={store('Globo').pk}&punto={self.napoli.pk}", data)
        status = SupplyStatus.objects.get()
        self.assertEqual((status.store, status.franchise), (store("Milanofiori"), None))

    def test_a_manager_never_sees_the_other_centers_or_the_franchises(self):
        self.status(self.scotch, RED, place=store("Globo"), note="nota di Globo")
        self.status(self.scotch, YELLOW, place=self.napoli, note="nota di Napoli")
        response = self.client.get(URL)
        for text in ("nota di Globo", "nota di Napoli", "Globo", "Punto Napoli"):
            self.assertNotContains(response, text)
        self.assertEqual(response.context["form"][f"level_{self.scotch.pk}"].value(), None)
        self.client.post(URL, self.form_data(**{f"level_{self.scotch.pk}": "green"}))
        self.assertEqual(SupplyStatus.objects.get(store=store("Globo")).level, RED)  # quella di Globo non cambia

    def test_inactive_resources_disappear(self):
        SupplyItem.objects.filter(pk=self.scotch.pk).update(active=False)
        response = self.client.get(URL)
        self.assertNotContains(response, f'name="level_{self.scotch.pk}"')
        self.client.post(URL, {f"level_{self.scotch.pk}": "red"})
        self.assertFalse(SupplyStatus.objects.filter(item=self.scotch).exists())

    def test_the_home_of_a_manager_shows_only_the_own_counts(self):
        self.status(self.scotch, RED)
        self.status(self.pallet, YELLOW)
        self.status(self.sticker30, RED, place=store("Globo"))
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.context["supply_totals"], {"red": 1, "yellow": 1})
        self.assertContains(response, "Aggiorna pallet, sticker e scotch")
        self.assertContains(response, f'href="{URL}"')


class FranchiseeTests(SuppliesCase):
    def setUp(self):
        self.client.force_login(self.anna)

    def test_a_franchisee_updates_the_own_point(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["place"], self.napoli)
        self.client.post(URL, {f"level_{self.pallet.pk}": "red", f"quantity_{self.pallet.pk}": "0"})
        status = SupplyStatus.objects.get()
        self.assertEqual((status.store, status.franchise, status.level, status.quantity), (None, self.napoli, RED, 0))

    def test_a_franchisee_never_sees_the_centers(self):
        self.status(self.scotch, RED, note="nota di Milanofiori")
        response = self.client.get(URL)
        self.assertNotContains(response, "nota di Milanofiori")
        self.assertNotContains(response, "Milanofiori")
        self.assertNotContains(response, 'class="dock"')

    def test_the_franchise_page_links_to_the_resources(self):
        self.assertContains(self.client.get(reverse("inventory:franchise")), f'href="{URL}"')


class OwnerTests(SuppliesCase):
    def setUp(self):
        self.client.force_login(self.tom)

    def fill(self):
        self.status(self.pallet, GREEN, quantity=6)
        self.status(self.scotch, RED, note="finito ieri")
        self.status(self.sticker30, YELLOW, place=store("Globo"))
        self.status(self.pallet, RED, place=self.napoli, quantity=0)

    def test_owners_see_every_center_and_point_read_only(self):
        self.fill()
        response = self.client.get(URL)
        self.assertTrue(response.context["overview"])
        titles = [group["title"] for group in response.context["groups"]]
        self.assertEqual(titles, ["Centri commerciali", "Franchising"])
        places = [card["place"].name for group in response.context["groups"] for card in group["cards"]]
        self.assertEqual(places, ["Globo", "Milanofiori", "Tor Vergata", "Punto Napoli"])
        for text in ("finito ieri", "6 pallet", "0 pallet", "In esaurimento", "non indicato", "solo lettura"):
            self.assertContains(response, text)
        self.assertNotContains(response, 'name="level_')
        self.assertEqual(response.context["totals"], {"red": 2, "yellow": 1})

    def test_owners_cannot_change_anything(self):
        response = self.client.post(URL, {f"level_{self.pallet.pk}": "red"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SupplyStatus.objects.exists())

    def test_filters_by_kind_and_problems(self):
        self.fill()
        centers = self.client.get(URL, {"tipo": "centri"})
        self.assertEqual([g["title"] for g in centers.context["groups"]], ["Centri commerciali"])
        franchises = self.client.get(URL, {"tipo": "franchising"})
        self.assertEqual([g["title"] for g in franchises.context["groups"]], ["Franchising"])
        self.assertNotContains(franchises, "Milanofiori")
        problems = self.client.get(URL, {"solo": "problemi"})
        rows = {
            card["place"].name: [(it.name, st.level) for it, st in card["rows"]]
            for group in problems.context["groups"] for card in group["cards"]
        }
        self.assertEqual(
            rows,
            {"Globo": [("Sticker 30%", YELLOW)], "Milanofiori": [("Scotch", RED)], "Punto Napoli": [("Pallet / merce", RED)]},
        )
        unknown = self.client.get(URL, {"tipo": "boh"})
        self.assertEqual(unknown.context["kind"], "")

    def test_the_owner_home_has_the_counts_and_the_link(self):
        self.fill()
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.context["supply_totals"], {"red": 2, "yellow": 1})
        self.assertContains(response, f"{URL}?solo=problemi")
        store_page = self.client.get(reverse("inventory:store", args=[store("Globo").pk]))
        self.assertEqual(store_page.context["supply_totals"], {"red": 0, "yellow": 1})

    def test_inactive_resources_are_not_counted(self):
        self.fill()
        SupplyItem.objects.filter(pk=self.scotch.pk).update(active=False)
        self.assertEqual(self.client.get(URL).context["totals"], {"red": 1, "yellow": 1})


class AccessTests(SuppliesCase):
    def test_login_and_role_are_needed(self):
        self.client.logout()
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("login")))
        self.client.force_login(make_user("nessuno"))
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.client.post(URL, {f"level_{self.pallet.pk}": "red"}).status_code, 403)
        self.assertFalse(SupplyStatus.objects.exists())
