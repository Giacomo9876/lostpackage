"""Pannello admin: creare un utente insieme al suo ruolo e al suo centro."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import Profile
from .helpers import PASSWORD, store

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
