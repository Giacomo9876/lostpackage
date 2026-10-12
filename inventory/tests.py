import shutil
import tempfile
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .models import Batch, Package, ShopSettings

TEMP_MEDIA = tempfile.mkdtemp(prefix="pacchi-test-media-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA, ignore_errors=True)


def make_image(size=(800, 600), name="IMG_0001.jpg", orientation=None, fmt="JPEG"):
    """Genera una foto in memoria, opzionalmente con l'orientamento EXIF come fanno i telefoni."""
    img = Image.new("RGB", size, "tomato")
    buffer = BytesIO()
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    img.save(buffer, fmt, **kwargs)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


def create_package(weight, batch=None, notes="", sold_price=None, sold_at=None):
    package = Package.objects.create(
        weight_kg=Decimal(str(weight)),
        batch=batch,
        notes=notes,
        photo=make_image(),
        thumb=make_image((100, 100)),
    )
    if sold_price is not None:
        package.sold_price = Decimal(str(sold_price))
        package.sold_at = sold_at or timezone.now()
        package.save()
    return package


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("marco", password="una-password-lunga-123")

    def setUp(self):
        self.client.force_login(self.user)


class AccessTests(TestCase):
    """Tutto richiede il login, tranne la pagina di accesso."""

    @override_settings(MEDIA_ROOT=TEMP_MEDIA)
    def test_anonymous_is_redirected_to_login_everywhere(self):
        package = create_package(1)
        batch = Batch.objects.create(name="A", expected_count=2)
        urls = [
            reverse("inventory:list"),
            reverse("inventory:history"),
            reverse("inventory:create"),
            reverse("inventory:batch_list"),
            reverse("inventory:batch_create"),
            reverse("inventory:batch_update", args=[batch.pk]),
            reverse("inventory:price"),
            reverse("inventory:detail", args=[package.pk]),
            reverse("inventory:update", args=[package.pk]),
            reverse("inventory:sell", args=[package.pk]),
            reverse("inventory:restore", args=[package.pk]),
            reverse("inventory:delete", args=[package.pk]),
            reverse("inventory:image", args=[package.pk, "foto"]),
            reverse("inventory:image", args=[package.pk, "miniatura"]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_login_page_and_admin_login_are_reachable(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_login_and_logout(self):
        get_user_model().objects.create_user("anna", password="una-password-lunga-123")
        response = self.client.post(reverse("login"), {"username": "anna", "password": "sbagliata"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error")

        response = self.client.post(reverse("login"), {"username": "anna", "password": "una-password-lunga-123"})
        self.assertRedirects(response, reverse("inventory:list"), fetch_redirect_response=False)

        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)  # solo POST
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(reverse("inventory:list")).status_code, 302)

    def test_anonymous_cannot_sell_restore_or_delete(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            package = create_package(1)
            sold = create_package(2, sold_price=10)
        self.client.post(reverse("inventory:sell", args=[package.pk]), {"sold_price": "10"})
        self.client.post(reverse("inventory:restore", args=[sold.pk]))
        self.client.post(reverse("inventory:delete", args=[package.pk]))
        package.refresh_from_db()
        sold.refresh_from_db()
        self.assertIsNone(package.sold_at)
        self.assertIsNotNone(sold.sold_at)


class CreateTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.batch = Batch.objects.create(name="Lotto 1", expected_count=5)

    def post(self, **overrides):
        data = {"photo": make_image(), "weight_kg": "2,35", "batch": self.batch.pk, "notes": ""}
        data.update(overrides)
        return self.client.post(reverse("inventory:create"), data)

    def test_create_accepts_comma_and_dot_decimals(self):
        for raw in ("2,35", "2.35"):
            with self.subTest(raw=raw):
                response = self.post(weight_kg=raw)
                self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        self.assertEqual(Package.objects.filter(weight_kg=Decimal("2.350")).count(), 2)

    def test_photo_is_reencoded_resized_and_renamed(self):
        self.post(photo=make_image((4000, 3000), name="../../evil.php.jpg"))
        package = Package.objects.get()
        photo, thumb = Image.open(package.photo.path), Image.open(package.thumb.path)
        self.assertEqual(photo.format, "JPEG")
        self.assertEqual(max(photo.size), 1600)
        self.assertEqual(max(thumb.size), 480)
        self.assertNotIn("evil", package.photo.name)
        self.assertTrue(package.photo.name.endswith(".jpg"))
        self.assertNotIn("exif", photo.info)

    def test_exif_orientation_is_applied(self):
        # Foto "orizzontale" con flag di rotazione: sul telefono appare verticale.
        self.post(photo=make_image((800, 600), orientation=6))
        photo = Image.open(Package.objects.get().photo.path)
        self.assertEqual(photo.size, (600, 800))

    def test_png_with_transparency_is_flattened(self):
        self.post(photo=make_image((200, 200), name="foto.png", fmt="PNG"))
        self.assertEqual(Image.open(Package.objects.get().photo.path).mode, "RGB")

    def test_non_image_is_rejected(self):
        fake = SimpleUploadedFile("foto.jpg", b"<?php echo 'hi'; ?>", content_type="image/jpeg")
        response = self.post(photo=fake)
        self.assertEqual(response.status_code, 200)
        self.assertIn("photo", response.context["form"].errors)
        self.assertEqual(Package.objects.count(), 0)

    @override_settings(MAX_UPLOAD_MB=0)
    def test_oversized_upload_is_rejected(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("photo", response.context["form"].errors)
        self.assertEqual(Package.objects.count(), 0)

    def test_photo_is_required_on_create(self):
        response = self.client.post(reverse("inventory:create"), {"weight_kg": "1,5", "batch": self.batch.pk})
        self.assertIn("photo", response.context["form"].errors)

    def test_invalid_weights_are_rejected(self):
        for raw in ("", "0", "-1", "abc", "1001", "2,3456"):
            with self.subTest(raw=raw):
                response = self.post(weight_kg=raw)
                self.assertEqual(response.status_code, 200)
                self.assertIn("weight_kg", response.context["form"].errors)
        self.assertEqual(Package.objects.count(), 0)

    def test_new_package_is_in_stock_not_sold(self):
        self.post()
        package = Package.objects.get()
        self.assertFalse(package.is_sold)
        self.assertEqual(package.created_by, self.user)

    def test_lot_is_required(self):
        response = self.post(batch="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("batch", response.context["form"].errors)
        self.assertEqual(Package.objects.count(), 0)

    def test_save_and_register_another_stays_on_same_lot(self):
        response = self.post(again="1")
        self.assertRedirects(
            response, f"{reverse('inventory:create')}?lotto={self.batch.pk}", fetch_redirect_response=False
        )

    def test_plain_save_goes_to_detail(self):
        response = self.post()
        self.assertRedirects(response, reverse("inventory:detail", args=[Package.objects.get().pk]))

    def test_lot_query_param_preselects_lot_and_shows_progress(self):
        create_package(1, self.batch)
        create_package(1, self.batch)
        response = self.client.get(reverse("inventory:create"), {"lotto": self.batch.pk})
        self.assertEqual(response.context["form"].initial["batch"], self.batch.pk)
        self.assertEqual((response.context["registered"], response.context["next_number"]), (2, 3))
        self.assertContains(response, "pacco 3 di 5")
        self.assertContains(response, "<progress")

    def test_progress_counts_sold_packages_too(self):
        create_package(1, self.batch)
        create_package(1, self.batch, sold_price=25)
        response = self.client.get(reverse("inventory:create"), {"lotto": self.batch.pk})
        self.assertEqual(response.context["registered"], 2)

    def test_last_lot_is_remembered(self):
        other = Batch.objects.create(name="Lotto 2", expected_count=3)
        self.post(batch=other.pk)
        response = self.client.get(reverse("inventory:create"))
        self.assertEqual(response.context["form"].initial["batch"], other.pk)

    def test_garbage_lot_param_is_ignored(self):
        for raw in ("abc", "99999", "1;drop"):
            with self.subTest(raw=raw):
                self.assertEqual(self.client.get(reverse("inventory:create"), {"lotto": raw}).status_code, 200)

    def test_completing_a_lot_returns_to_lot_list(self):
        small = Batch.objects.create(name="Piccolo", expected_count=2)
        first = self.post(batch=small.pk, again="1")
        self.assertIn(f"lotto={small.pk}", first["Location"])
        second = self.post(batch=small.pk, again="1")
        self.assertRedirects(second, reverse("inventory:batch_list"), fetch_redirect_response=False)
        followed = self.client.get(second["Location"])
        self.assertContains(followed, "completo: 2 di 2 pacchi registrati")

    def test_going_over_the_expected_count_is_allowed_and_raises_the_total(self):
        small = Batch.objects.create(name="Piccolo", expected_count=1)
        create_package(1, small)
        response = self.client.get(reverse("inventory:create"), {"lotto": small.pk})
        self.assertTrue(response.context["complete"])
        self.assertContains(response, "è completo")
        self.assertContains(response, "il totale sale da solo")
        extra = self.post(batch=small.pk, again="1")
        self.assertEqual(small.packages.count(), 2)
        small.refresh_from_db()
        self.assertEqual(small.expected_count, 2)
        # non è un vicolo cieco: si resta nel modulo dello stesso lotto, con l'avviso del nuovo totale
        self.assertIn(f"lotto={small.pk}", extra["Location"])
        self.assertContains(self.client.get(extra["Location"]), "ora ha 2 pacchi (ne erano previsti 1)")

    def test_lot_without_total_never_completes_and_keeps_going(self):
        open_lot = Batch.objects.create(name="Aperto")
        for _ in range(3):
            response = self.post(batch=open_lot.pk, again="1")
            self.assertIn(f"lotto={open_lot.pk}", response["Location"])
        open_lot.refresh_from_db()
        self.assertEqual((open_lot.packages.count(), open_lot.expected_count), (3, 0))

    def test_register_page_links_to_total_editing(self):
        response = self.client.get(reverse("inventory:create"), {"lotto": self.batch.pk})
        self.assertContains(response, f"{reverse('inventory:batch_update', args=[self.batch.pk])}?da=registra")
        self.assertContains(response, "Cambia il totale")
        open_lot = Batch.objects.create(name="Aperto")
        self.assertContains(self.client.get(reverse("inventory:create"), {"lotto": open_lot.pk}), "Indica il totale")

    def test_without_any_lot_you_are_sent_to_create_one(self):
        Batch.objects.all().delete()
        response = self.client.get(reverse("inventory:create"))
        self.assertRedirects(response, reverse("inventory:batch_create"))


class BatchTests(BaseCase):
    def test_create_lot_with_expected_count_then_register_its_packages(self):
        response = self.client.post(reverse("inventory:batch_create"), {"name": "Lotto 1", "expected_count": "10"})
        batch = Batch.objects.get()
        self.assertEqual(batch.expected_count, 10)
        self.assertRedirects(
            response, f"{reverse('inventory:create')}?lotto={batch.pk}", fetch_redirect_response=False
        )
        followed = self.client.get(response["Location"])
        self.assertContains(followed, "pacco 1 di 10")
        self.assertContains(followed, "ora registra i suoi 10 pacchi")

    def test_name_is_suggested_and_skips_existing_ones(self):
        self.assertEqual(self.client.get(reverse("inventory:batch_create")).context["form"].initial["name"], "Lotto 1")
        Batch.objects.create(name="Lotto 1", expected_count=3)
        self.assertEqual(self.client.get(reverse("inventory:batch_create")).context["form"].initial["name"], "Lotto 2")

    def test_duplicate_name_is_rejected_case_insensitively(self):
        Batch.objects.create(name="Lotto 1", expected_count=3)
        response = self.client.post(reverse("inventory:batch_create"), {"name": "lotto 1", "expected_count": "4"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Batch.objects.count(), 1)

    def test_expected_count_must_be_positive_when_given(self):
        for raw in ("0", "-3", "abc", "5000"):
            with self.subTest(raw=raw):
                response = self.client.post(reverse("inventory:batch_create"), {"name": "X", "expected_count": raw})
                self.assertEqual(response.status_code, 200)
                self.assertIn("expected_count", response.context["form"].errors)
        self.assertEqual(Batch.objects.count(), 0)

    def test_expected_count_can_be_left_empty_at_creation(self):
        response = self.client.post(reverse("inventory:batch_create"), {"name": "Lotto 1", "expected_count": ""})
        batch = Batch.objects.get()
        self.assertEqual(batch.expected_count, 0)
        self.assertRedirects(
            response, f"{reverse('inventory:create')}?lotto={batch.pk}", fetch_redirect_response=False
        )
        followed = self.client.get(response["Location"])
        self.assertContains(followed, "il totale lo potrai indicare più avanti")
        self.assertContains(followed, "0 pacchi registrati finora")
        self.assertNotContains(followed, "<progress")

    def test_total_can_be_set_later_while_registering(self):
        batch = Batch.objects.create(name="Lotto 1")
        create_package(1, batch)
        url = f"{reverse('inventory:batch_update', args=[batch.pk])}?da=registra"
        response = self.client.post(url, {"name": "Lotto 1", "expected_count": "12"})
        self.assertRedirects(
            response, f"{reverse('inventory:create')}?lotto={batch.pk}", fetch_redirect_response=False
        )
        batch.refresh_from_db()
        self.assertEqual(batch.expected_count, 12)
        self.assertContains(self.client.get(response["Location"]), "pacco 2 di 12")

    def test_total_can_be_cleared_again(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=5)
        self.client.post(reverse("inventory:batch_update", args=[batch.pk]), {"name": "Lotto 1", "expected_count": ""})
        batch.refresh_from_db()
        self.assertEqual(batch.expected_count, 0)

    def test_total_cannot_be_lower_than_already_registered(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=10)
        for _ in range(4):
            create_package(1, batch)
        response = self.client.post(
            reverse("inventory:batch_update", args=[batch.pk]), {"name": "Lotto 1", "expected_count": "3"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("expected_count", response.context["form"].errors)
        batch.refresh_from_db()
        self.assertEqual(batch.expected_count, 10)
        ok = self.client.post(
            reverse("inventory:batch_update", args=[batch.pk]), {"name": "Lotto 1", "expected_count": "4"}
        )
        self.assertEqual(ok.status_code, 302)

    def test_return_parameter_is_not_an_open_redirect(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=3)
        url = f"{reverse('inventory:batch_update', args=[batch.pk])}?da=https://example.com"
        response = self.client.post(url, {"name": "Lotto 1", "expected_count": "3"})
        self.assertRedirects(response, reverse("inventory:batch_list"), fetch_redirect_response=False)

    def test_list_shows_progress_and_stock_and_sold_counts(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=5)
        create_package(1, batch)
        create_package(1, batch)
        create_package(1, batch, sold_price=25)
        response = self.client.get(reverse("inventory:batch_list"))
        row = response.context["batches"][0]
        self.assertEqual((row.registered, row.in_stock, row.sold), (3, 2, 1))
        self.assertContains(response, "3 di 5 registrati")
        self.assertContains(response, "2 in magazzino, 1 venduto")
        self.assertNotContains(response, "Completo")

    def test_complete_lot_is_marked_but_still_lets_you_add_a_package(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=1)
        create_package(1, batch)
        response = self.client.get(reverse("inventory:batch_list"))
        self.assertContains(response, "Completo")
        self.assertContains(response, "Aggiungi un pacco")
        self.assertContains(response, f"{reverse('inventory:create')}?lotto={batch.pk}")

    def test_lot_without_total_is_listed_and_flagged(self):
        batch = Batch.objects.create(name="Vecchio")  # expected_count = 0
        create_package(1, batch)
        response = self.client.get(reverse("inventory:batch_list"))
        self.assertContains(response, "1 registrato")
        self.assertContains(response, "Totale da indicare")
        self.assertContains(response, "Registra pacchi")
        self.assertNotContains(response, "<progress")

    def test_edit_lot(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=3)
        response = self.client.post(
            reverse("inventory:batch_update", args=[batch.pk]), {"name": "Lotto uno", "expected_count": "8"}
        )
        self.assertRedirects(response, reverse("inventory:batch_list"))
        batch.refresh_from_db()
        self.assertEqual((batch.name, batch.expected_count), ("Lotto uno", 8))

    def test_renaming_a_lot_to_itself_is_allowed(self):
        batch = Batch.objects.create(name="Lotto 1", expected_count=3)
        response = self.client.post(
            reverse("inventory:batch_update", args=[batch.pk]), {"name": "Lotto 1", "expected_count": "4"}
        )
        self.assertEqual(response.status_code, 302)


class SearchTests(BaseCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            cls.batch_a = Batch.objects.create(name="A", expected_count=10)
            cls.batch_b = Batch.objects.create(name="B", expected_count=10)
            cls.same = [create_package("2.350", cls.batch_a, f"gemello {i}") for i in range(3)]
            cls.near = create_package("2.380", cls.batch_b)
            cls.far = create_package("2.500", cls.batch_b)
            cls.heavy = create_package("5.000", cls.batch_a)

    def search(self, **params):
        return self.client.get(reverse("inventory:list"), params)

    def test_without_filters_lists_everything_in_stock(self):
        response = self.search()
        self.assertEqual(len(response.context["packages"]), 6)
        self.assertEqual(response.context["total_weight"], Decimal("16.930"))

    def test_exact_search_finds_all_packages_with_same_weight(self):
        response = self.search(peso="2,35", tolleranza="0")
        self.assertEqual({p.pk for p in response.context["packages"]}, {p.pk for p in self.same})
        self.assertContains(response, "Stesso peso", count=3)

    def test_tolerance_orders_by_closeness_with_stable_ties(self):
        response = self.search(peso="2.35", tolleranza="50")
        packages = list(response.context["packages"])
        self.assertEqual([p.pk for p in packages], [p.pk for p in self.same] + [self.near.pk])
        self.assertEqual([p.delta_g for p in packages], [0, 0, 0, 30])

    def test_wider_tolerance_includes_more(self):
        self.assertEqual(len(self.search(peso="2,35", tolleranza="250").context["packages"]), 5)
        self.assertEqual(len(self.search(peso="2,35", tolleranza="25").context["packages"]), 3)

    def test_tolerance_defaults_to_50_grams(self):
        self.assertEqual(len(self.search(peso="2,35").context["packages"]), 4)

    def test_search_below_target_has_negative_delta(self):
        packages = list(self.search(peso="2,4", tolleranza="100").context["packages"])
        # 2,380 è a -20 g; i tre da 2,350 a -50 g; 2,500 a +100 g (ordinati per distanza).
        self.assertEqual([p.delta_g for p in packages], [-20, -50, -50, -50, 100])

    def test_filter_by_lot(self):
        response = self.search(batch=self.batch_b.pk)
        self.assertEqual({p.pk for p in response.context["packages"]}, {self.near.pk, self.far.pk})

    def test_weight_and_lot_combined(self):
        response = self.search(peso="2,35", tolleranza="250", batch=self.batch_b.pk)
        self.assertEqual({p.pk for p in response.context["packages"]}, {self.near.pk, self.far.pk})

    def test_invalid_search_shows_error_not_everything(self):
        response = self.search(peso="abc")
        self.assertEqual(len(response.context["packages"]), 0)
        self.assertIn("peso", response.context["form"].errors)

    def test_no_match_shows_helpful_message(self):
        self.assertContains(self.search(peso="9,9", tolleranza="0"), "Nessun pacco trovato")

    def test_pagination_keeps_filters(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            for _ in range(30):
                create_package("1.000", self.batch_a)
        page1 = self.search(peso="1", tolleranza="0")
        self.assertEqual(len(page1.context["packages"]), 24)
        self.assertContains(page1, "peso=1")
        self.assertContains(page1, "page=2")
        self.assertEqual(len(self.search(peso="1", tolleranza="0", page=2).context["packages"]), 6)


class PriceTests(BaseCase):
    def test_default_price_is_25_euro_per_kg(self):
        self.assertEqual(ShopSettings.load().price_per_kg, Decimal("25.00"))

    def test_stock_list_shows_price_from_weight(self):
        create_package("2.350")
        response = self.client.get(reverse("inventory:list"))
        self.assertEqual(response.context["packages"][0].price, Decimal("58.75"))
        self.assertContains(response, "58,75 €")
        self.assertContains(response, "25,00 € al kg")

    def test_stock_value_summary(self):
        create_package("2.000")
        create_package("1.500")
        response = self.client.get(reverse("inventory:list"))
        self.assertEqual(response.context["stock_value"], Decimal("87.50"))
        self.assertContains(response, "valore 87,50 €")

    def test_price_rounds_to_the_cent(self):
        package = create_package("1.333")
        self.assertEqual(package.price_at(Decimal("25.00")), Decimal("33.33"))
        self.assertEqual(create_package("0.001").price_at(Decimal("25.00")), Decimal("0.03"))  # 0,025 arrotonda su

    def test_price_can_be_changed_with_comma_or_dot(self):
        for raw, expected in (("27,5", "27.50"), ("30.00", "30.00")):
            with self.subTest(raw=raw):
                response = self.client.post(reverse("inventory:price"), {"price_per_kg": raw})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(ShopSettings.load().price_per_kg, Decimal(expected))

    def test_invalid_prices_are_rejected(self):
        for raw in ("", "0", "-5", "abc"):
            with self.subTest(raw=raw):
                response = self.client.post(reverse("inventory:price"), {"price_per_kg": raw})
                self.assertEqual(response.status_code, 200)
                self.assertIn("price_per_kg", response.context["form"].errors)
        self.assertEqual(ShopSettings.load().price_per_kg, Decimal("25.00"))

    def test_new_price_applies_to_stock_but_not_to_past_sales(self):
        package = create_package("2.000")
        sold = create_package("2.000", sold_price="50.00")
        self.client.post(reverse("inventory:price"), {"price_per_kg": "30"})

        stock = self.client.get(reverse("inventory:list"))
        self.assertEqual(stock.context["packages"][0].price, Decimal("60.00"))
        history = self.client.get(reverse("inventory:history"))
        self.assertEqual(history.context["revenue"], Decimal("50.00"))
        sold.refresh_from_db()
        self.assertEqual(sold.sold_price, Decimal("50.00"))
        self.assertTrue(package.pk)

    def test_price_form_returns_to_the_stock_search(self):
        self.client.get(reverse("inventory:list"), {"peso": "2,35"})
        response = self.client.post(reverse("inventory:price"), {"price_per_kg": "26"})
        self.assertIn("peso=2%2C35", response["Location"])


class SaleTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.batch = Batch.objects.create(name="Lotto 1", expected_count=5)

    def sell(self, package, price):
        return self.client.post(reverse("inventory:sell", args=[package.pk]), {"sold_price": price})

    def test_sale_form_is_prefilled_with_weight_times_price(self):
        package = create_package("2.350", self.batch)
        response = self.client.get(reverse("inventory:sell", args=[package.pk]))
        self.assertContains(response, 'value="58,75"')
        self.assertContains(response, "2,350 kg × 25,00 € = 58,75 €")

    def test_sale_moves_the_package_to_history(self):
        package = create_package("2.350", self.batch)
        response = self.sell(package, "58,75")
        self.assertEqual(response.status_code, 302)
        package.refresh_from_db()
        self.assertTrue(package.is_sold)
        self.assertEqual(package.sold_price, Decimal("58.75"))
        self.assertEqual(package.sold_by, self.user)
        self.assertLess((timezone.now() - package.sold_at).total_seconds(), 60)
        # Non è più in magazzino ma è nello storico.
        self.assertEqual(len(self.client.get(reverse("inventory:list")).context["packages"]), 0)
        self.assertEqual([p.pk for p in self.client.get(reverse("inventory:history")).context["packages"]], [package.pk])

    def test_price_can_be_edited_at_sale(self):
        package = create_package("2.000", self.batch)
        self.sell(package, "40")
        package.refresh_from_db()
        self.assertEqual(package.sold_price, Decimal("40.00"))

    def test_free_sale_is_allowed_but_not_negative_or_empty(self):
        package = create_package("2.000", self.batch)
        for raw in ("", "-1", "abc", "1234567,891"):
            with self.subTest(raw=raw):
                response = self.sell(package, raw)
                self.assertEqual(response.status_code, 200)
                self.assertIn("sold_price", response.context["form"].errors)
        package.refresh_from_db()
        self.assertFalse(package.is_sold)
        self.assertEqual(self.sell(package, "0").status_code, 302)

    def test_photos_are_kept_after_sale(self):
        package = create_package("2.350", self.batch)
        names = [package.photo.name, package.thumb.name]
        with self.captureOnCommitCallbacks(execute=True):
            self.sell(package, "58,75")
        self.assertTrue(all(package.photo.storage.exists(n) for n in names))
        response = self.client.get(reverse("inventory:image", args=[package.pk, "foto"]))
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_after_selling_you_return_to_the_same_search(self):
        a, b = create_package("2.350", self.batch), create_package("2.350", self.batch)
        self.client.get(reverse("inventory:list"), {"peso": "2,35", "tolleranza": "0"})
        response = self.sell(a, "58,75")
        self.assertIn("peso=2%2C35", response["Location"])
        followed = self.client.get(response["Location"])
        self.assertEqual([p.pk for p in followed.context["packages"]], [b.pk])
        self.assertContains(followed, "venduto a 58,75 €")

    def test_sold_package_is_excluded_from_stock_search_but_found_in_history(self):
        package = create_package("3.000", self.batch)
        self.sell(package, "75")
        stock = self.client.get(reverse("inventory:list"), {"peso": "3", "tolleranza": "0"})
        self.assertEqual(len(stock.context["packages"]), 0)
        history = self.client.get(reverse("inventory:history"), {"peso": "3", "tolleranza": "0"})
        self.assertEqual([p.pk for p in history.context["packages"]], [package.pk])

    def test_correcting_the_price_keeps_sale_date_and_seller(self):
        package = create_package("2.000", self.batch)
        self.sell(package, "50")
        package.refresh_from_db()
        sold_at = package.sold_at
        other = get_user_model().objects.create_user("anna", password="una-password-lunga-123")
        self.client.force_login(other)

        response = self.sell(package, "45")
        self.assertRedirects(response, reverse("inventory:detail", args=[package.pk]))
        package.refresh_from_db()
        self.assertEqual(package.sold_price, Decimal("45.00"))
        self.assertEqual(package.sold_at, sold_at)
        self.assertEqual(package.sold_by, self.user)

    def test_sold_price_is_not_touched_by_price_per_kg_changes(self):
        package = create_package("2.000", self.batch)
        self.sell(package, "50")
        settings_row = ShopSettings.load()
        settings_row.price_per_kg = Decimal("99.00")
        settings_row.save()
        package.refresh_from_db()
        self.assertEqual(package.sold_price, Decimal("50.00"))
        response = self.client.get(reverse("inventory:sell", args=[package.pk]))
        self.assertContains(response, 'value="50,00"')

    def test_restore_puts_the_package_back_in_stock(self):
        package = create_package("2.350", self.batch)
        self.sell(package, "58,75")
        response = self.client.post(reverse("inventory:restore", args=[package.pk]))
        self.assertRedirects(response, reverse("inventory:detail", args=[package.pk]))
        package.refresh_from_db()
        self.assertIsNone(package.sold_at)
        self.assertIsNone(package.sold_price)
        self.assertIsNone(package.sold_by)
        self.assertEqual(len(self.client.get(reverse("inventory:list")).context["packages"]), 1)
        self.assertEqual(len(self.client.get(reverse("inventory:history")).context["packages"]), 0)

    def test_restore_needs_post_and_a_sold_package(self):
        package = create_package("1.000", self.batch)
        url = reverse("inventory:restore", args=[package.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 404)  # non è venduto


class HistoryTests(BaseCase):
    def test_empty_history_invites_to_sell(self):
        self.assertContains(self.client.get(reverse("inventory:history")), "Ancora nessuna vendita")

    def test_totals_and_order(self):
        old = create_package("2.000", sold_price="50.00", sold_at=timezone.now() - timezone.timedelta(days=2))
        new = create_package("1.500", sold_price="37.50")
        create_package("9.000")  # in magazzino: non conta
        response = self.client.get(reverse("inventory:history"))
        self.assertEqual([p.pk for p in response.context["packages"]], [new.pk, old.pk])  # prima il più recente
        self.assertEqual(response.context["revenue"], Decimal("87.50"))
        self.assertEqual(response.context["total_weight"], Decimal("3.500"))
        self.assertContains(response, "2 pacchi venduti")
        self.assertContains(response, "incasso 87,50 €")

    def test_history_shows_sold_price_not_current_price(self):
        create_package("2.000", sold_price="42.00")
        response = self.client.get(reverse("inventory:history"))
        self.assertContains(response, "42,00 €")
        self.assertNotContains(response, "50,00 €")

    def test_history_filters_by_lot(self):
        a, b = Batch.objects.create(name="A"), Batch.objects.create(name="B")
        in_a = create_package("1.000", a, sold_price=25)
        create_package("1.000", b, sold_price=25)
        response = self.client.get(reverse("inventory:history"), {"batch": a.pk})
        self.assertEqual([p.pk for p in response.context["packages"]], [in_a.pk])
        self.assertEqual(response.context["revenue"], Decimal("25.00"))

    def test_history_and_stock_remember_their_own_searches(self):
        package = create_package("2.000", sold_price="50")
        self.client.get(reverse("inventory:list"), {"peso": "1"})
        self.client.get(reverse("inventory:history"), {"peso": "2"})
        response = self.client.get(reverse("inventory:detail", args=[package.pk]))
        self.assertContains(response, f"{reverse('inventory:history')}?peso=2")
        self.assertContains(response, "Torna allo storico")


class UpdateTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.batch = Batch.objects.create(name="Lotto 1", expected_count=5)

    def data(self, **overrides):
        data = {"weight_kg": "1,250", "batch": self.batch.pk, "notes": "corretto"}
        data.update(overrides)
        return data

    def test_edit_weight_keeps_photo(self):
        package = create_package("1.000", self.batch)
        old_photo = package.photo.name
        response = self.client.post(reverse("inventory:update", args=[package.pk]), self.data())
        self.assertEqual(response.status_code, 302)
        package.refresh_from_db()
        self.assertEqual(package.weight_kg, Decimal("1.250"))
        self.assertEqual(package.photo.name, old_photo)
        self.assertTrue(package.photo.storage.exists(old_photo))

    def test_replacing_photo_removes_old_files(self):
        package = create_package("1.000", self.batch)
        old = [package.photo.name, package.thumb.name]
        self.client.post(
            reverse("inventory:update", args=[package.pk]), self.data(photo=make_image((300, 300)))
        )
        package.refresh_from_db()
        self.assertNotIn(package.photo.name, old)
        for name in old:
            self.assertFalse(package.photo.storage.exists(name))
        self.assertTrue(package.photo.storage.exists(package.photo.name))

    def test_edit_form_shows_localized_weight(self):
        package = create_package("2.350", self.batch)
        response = self.client.get(reverse("inventory:update", args=[package.pk]))
        self.assertContains(response, 'value="2,350"')

    def test_editing_a_sold_package_keeps_it_sold(self):
        package = create_package("2.000", self.batch, sold_price="50")
        self.client.post(reverse("inventory:update", args=[package.pk]), self.data(weight_kg="2,100"))
        package.refresh_from_db()
        self.assertTrue(package.is_sold)
        self.assertEqual(package.sold_price, Decimal("50.00"))
        self.assertEqual(package.weight_kg, Decimal("2.100"))


class DeleteTests(BaseCase):
    """Eliminare è solo per i pacchi registrati per errore: i venduti vanno nello storico."""

    def test_get_asks_for_confirmation_and_does_not_delete(self):
        package = create_package("2.350")
        response = self.client.get(reverse("inventory:delete", args=[package.pk]))
        self.assertContains(response, "Eliminare il pacco")
        self.assertContains(response, "registrato per errore")
        self.assertTrue(Package.objects.filter(pk=package.pk).exists())

    def test_post_deletes_row_and_photo_files(self):
        package = create_package("2.350")
        names = [package.photo.name, package.thumb.name]
        storage = package.photo.storage
        self.assertTrue(all(storage.exists(n) for n in names))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("inventory:delete", args=[package.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Package.objects.filter(pk=package.pk).exists())
        self.assertFalse(any(storage.exists(n) for n in names))

    def test_deleted_by_mistake_does_not_appear_in_history(self):
        package = create_package("2.350")
        self.client.post(reverse("inventory:delete", args=[package.pk]))
        self.assertEqual(len(self.client.get(reverse("inventory:history")).context["packages"]), 0)

    def test_after_deleting_you_return_to_the_same_search(self):
        a, b = create_package("2.350"), create_package("2.350")
        self.client.get(reverse("inventory:list"), {"peso": "2,35", "tolleranza": "0"})
        response = self.client.post(reverse("inventory:delete", args=[a.pk]))
        self.assertIn("peso=2%2C35", response["Location"])
        followed = self.client.get(response["Location"])
        self.assertEqual([p.pk for p in followed.context["packages"]], [b.pk])

    def test_deleting_twice_gives_404(self):
        package = create_package("1")
        url = reverse("inventory:delete", args=[package.pk])
        self.client.post(url)
        self.assertEqual(self.client.post(url).status_code, 404)


class ImageViewTests(BaseCase):
    def test_serves_jpeg_privately(self):
        package = create_package("1")
        for kind in ("foto", "miniatura"):
            with self.subTest(kind=kind):
                response = self.client.get(reverse("inventory:image", args=[package.pk, kind]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/jpeg")
                self.assertIn("private", response["Cache-Control"])
                response.close()

    def test_unknown_kind_and_unknown_package_are_404(self):
        package = create_package("1")
        self.assertEqual(self.client.get(reverse("inventory:image", args=[package.pk, "originale"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("inventory:image", args=[9999, "foto"])).status_code, 404)

    def test_media_url_is_not_served(self):
        package = create_package("1")
        self.assertEqual(self.client.get("/media/" + package.photo.name).status_code, 404)


class DetailTests(BaseCase):
    def test_in_stock_detail_shows_price_and_sale_actions(self):
        batch = Batch.objects.create(name="Lotto X", expected_count=3)
        package = create_package("2.350", batch, "scatola rossa")
        response = self.client.get(reverse("inventory:detail", args=[package.pk]))
        self.assertContains(response, "2,350")
        self.assertContains(response, "58,75 €")
        self.assertContains(response, "Lotto X")
        self.assertContains(response, "scatola rossa")
        self.assertContains(response, reverse("inventory:sell", args=[package.pk]))
        self.assertContains(response, reverse("inventory:delete", args=[package.pk]))
        self.assertNotContains(response, reverse("inventory:restore", args=[package.pk]))

    def test_sold_detail_shows_sale_and_restore_but_no_delete(self):
        package = create_package("2.350", sold_price="55.00")
        response = self.client.get(reverse("inventory:detail", args=[package.pk]))
        self.assertContains(response, "Venduto")
        self.assertContains(response, "55,00 €")
        self.assertContains(response, reverse("inventory:restore", args=[package.pk]))
        self.assertNotContains(response, reverse("inventory:delete", args=[package.pk]))

    def test_notes_are_escaped(self):
        package = create_package("1", notes="<script>alert(1)</script>")
        response = self.client.get(reverse("inventory:detail", args=[package.pk]))
        self.assertNotContains(response, "<script>alert(1)</script>")


class LoginThrottleTests(TestCase):
    """Dopo troppe password sbagliate l'utente resta bloccato, anche con la password giusta."""

    PASSWORD = "una-password-lunga-123"

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.marco = User.objects.create_user("marco", password=cls.PASSWORD)
        cls.anna = User.objects.create_user("anna", password=cls.PASSWORD)

    def fail(self, username, times):
        for _ in range(times):
            self.client.post(reverse("login"), {"username": username, "password": "sbagliata"})

    def login(self, username, password=None):
        return self.client.post(reverse("login"), {"username": username, "password": password or self.PASSWORD})

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_user_is_locked_after_too_many_failures_even_with_the_right_password(self):
        self.fail("marco", 3)
        response = self.login("marco")
        self.assertEqual(response.status_code, 429)
        self.assertTemplateUsed(response, "registration/locked.html")
        self.assertContains(response, "Troppi tentativi", status_code=429)
        self.assertEqual(self.client.get(reverse("inventory:list")).status_code, 302)  # non è entrato

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_other_users_are_not_affected(self):
        self.fail("marco", 3)
        self.assertRedirects(self.login("anna"), reverse("inventory:list"), fetch_redirect_response=False)

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_below_the_limit_you_can_still_log_in_and_the_counter_resets(self):
        self.fail("marco", 2)
        self.assertEqual(self.login("marco").status_code, 302)
        self.client.post(reverse("logout"))
        self.fail("marco", 2)  # senza il reset sarebbero 4 in totale
        self.assertEqual(self.login("marco").status_code, 302)

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_unknown_usernames_are_throttled_too(self):
        self.fail("admin", 3)
        self.assertEqual(self.login("admin").status_code, 429)

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_lock_also_covers_the_admin_login(self):
        self.fail("marco", 3)
        response = self.client.post("/admin/login/", {"username": "marco", "password": self.PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_default_limit_is_five_failures(self):
        self.fail("marco", 4)
        self.assertEqual(self.login("marco").status_code, 302)
        self.client.post(reverse("logout"))
        self.fail("marco", 5)
        self.assertEqual(self.login("marco").status_code, 429)
