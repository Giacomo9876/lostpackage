"""Pannello admin: creare un utente insieme al suo ruolo e al suo centro."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Profile
from .helpers import PASSWORD, sell_on, store

User = get_user_model()


class AdminProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_superuser("capo", password=PASSWORD)

    def setUp(self):
        self.client.force_login(self.boss)

    def add_user(self, username, **profile):
        data = {
            "username": username,
            "password1": "Una-password-lunga-9",
            "password2": "Una-password-lunga-9",
            "profile-TOTAL_FORMS": "1",
            "profile-INITIAL_FORMS": "0",
            "profile-MIN_NUM_FORMS": "0",
            "profile-MAX_NUM_FORMS": "1",
        }
        data.update({f"profile-0-{k}": v for k, v in profile.items()})
        return self.client.post(reverse("admin:auth_user_add"), data)

    def test_admin_pages_open(self):
        for name in (
            "admin:auth_user_changelist",
            "admin:auth_user_add",
            "admin:inventory_store_changelist",
            "admin:inventory_batch_changelist",
            "admin:inventory_dayclose_changelist",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_packages_are_not_in_the_admin(self):
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, "/inventory/package/")

    def test_a_manager_can_be_created_with_a_center(self):
        response = self.add_user("lucia", role="manager", store=store("Globo").pk)
        self.assertEqual(response.status_code, 302)
        profile = Profile.objects.get(user__username="lucia")
        self.assertEqual((profile.role, profile.store), ("manager", store("Globo")))

    def test_a_manager_without_a_center_is_refused(self):
        response = self.add_user("senza", role="manager", store="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Profile.objects.filter(user__username="senza").exists())

    def test_an_owner_is_created_without_a_center(self):
        response = self.add_user("tom", role="owner", store="")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Profile.objects.get(user__username="tom").role, "owner")

    def test_the_user_list_shows_role_and_center(self):
        self.add_user("lucia", role="manager", store=store("Globo").pk)
        response = self.client.get(reverse("admin:auth_user_changelist"))
        self.assertContains(response, "Store manager")
        self.assertContains(response, "Globo")

    def test_new_users_are_not_staff(self):
        self.add_user("lucia", role="manager", store=store("Globo").pk)
        user = User.objects.get(username="lucia")
        self.assertFalse(user.is_staff or user.is_superuser)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)  # va al login dell'admin

    def test_the_role_form_is_always_on_the_add_page(self):
        response = self.client.get(reverse("admin:auth_user_add"))
        self.assertContains(response, "Ruolo nel gestionale")
        self.assertContains(response, 'name="profile-0-role"')

    def test_editing_an_old_user_without_a_role_asks_for_one(self):
        old = User.objects.create_user("vecchio", password=PASSWORD)
        response = self.client.get(reverse("admin:auth_user_change", args=[old.pk]))
        self.assertContains(response, "Ruolo nel gestionale")
        self.assertContains(response, 'name="profile-0-role"')


class TitolareAccountTests(TestCase):
    """Tom e Gabri: titolari che vedono tutto ma non hanno un'utenza superuser né entrano nell'admin."""

    @classmethod
    def setUpTestData(cls):
        cls.boss = User.objects.create_superuser("capo", password=PASSWORD)
        today = timezone.localdate()
        sell_on(today, "1", "25.00")
        sell_on(today, "2", "50.00", store_name="Globo", payment="yakuza")
        sell_on(today, "1", "25.00", store_name="Tor Vergata", payment="card")

    def create(self, username):
        self.client.force_login(self.boss)
        response = self.client.post(
            reverse("admin:auth_user_add"),
            {
                "username": username,
                "password1": "Una-password-lunga-9",
                "password2": "Una-password-lunga-9",
                "profile-TOTAL_FORMS": "1",
                "profile-INITIAL_FORMS": "0",
                "profile-MIN_NUM_FORMS": "0",
                "profile-MAX_NUM_FORMS": "1",
                "profile-0-role": "owner",
                "profile-0-store": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.client.logout()
        return User.objects.get(username=username)

    def test_titolari_are_neither_superuser_nor_staff(self):
        for name in ("tom", "gabri"):
            with self.subTest(name=name):
                user = self.create(name)
                self.assertFalse(user.is_superuser or user.is_staff)
                self.assertEqual(user.profile.role, Profile.Role.OWNER)
                self.assertIsNone(user.profile.store)

    def sign_in(self, username):
        """Il vero accesso dalla pagina di login (con axes), non una scorciatoia di test."""
        response = self.client.post(reverse("login"), {"username": username, "password": "Una-password-lunga-9"})
        return response.status_code == 302 and "_auth_user_id" in self.client.session

    def test_a_titolare_sees_every_center_prices_and_yakuza(self):
        self.create("tom")
        self.assertTrue(self.sign_in("tom"))
        overview = self.client.get(reverse("inventory:dashboard"), {"periodo": "oggi"})
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.context["total"].revenue, Decimal("100.00"))
        self.assertEqual(overview.context["total"].yakuza, Decimal("50.00"))
        self.assertEqual({c["store"].name for c in overview.context["cards"]}, {"Milanofiori", "Globo", "Tor Vergata"})
        for name in ("Milanofiori", "Globo", "Tor Vergata"):
            self.assertEqual(self.client.get(reverse("inventory:store", args=[store(name).pk])).status_code, 200)
        self.assertEqual(len(self.client.get(reverse("inventory:history")).context["packages"]), 3)
        self.assertEqual(self.client.get(reverse("inventory:price")).status_code, 200)

    def test_a_titolare_cannot_enter_the_admin_in_any_way(self):
        self.create("gabri")
        self.assertTrue(self.sign_in("gabri"))
        for name, args in (
            ("admin:index", []),
            ("admin:auth_user_changelist", []),
            ("admin:auth_user_add", []),
            ("admin:inventory_store_changelist", []),
            ("admin:auth_user_change", [self.boss.pk]),
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])
        # e non può nemmeno promuovere sé stesso scrivendo nell'admin
        self.client.post(reverse("admin:auth_user_change", args=[User.objects.get(username="gabri").pk]), {"is_superuser": "on", "is_staff": "on"})
        user = User.objects.get(username="gabri")
        self.assertFalse(user.is_superuser or user.is_staff)

    def test_the_two_titolari_are_independent_accounts(self):
        tom, gabri = self.create("tom"), self.create("gabri")
        self.assertNotEqual(tom.pk, gabri.pk)
        tom.is_active = False
        tom.save()
        self.assertFalse(self.sign_in("tom"))
        self.assertTrue(self.sign_in("gabri"))
        self.assertEqual(self.client.get(reverse("inventory:dashboard")).status_code, 200)
