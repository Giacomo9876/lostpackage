import atexit
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from ..models import Batch, DayClose, Package, Profile, ShopSettings, Store
from ..stats import start_of_day

TEMP_MEDIA = tempfile.mkdtemp(prefix="pacchi-test-media-")
atexit.register(shutil.rmtree, TEMP_MEDIA, ignore_errors=True)

PASSWORD = "una-password-lunga-123"


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


def store(name="Milanofiori"):
    """Uno dei tre centri creati dalla migrazione (Milanofiori, Globo, Tor Vergata)."""
    return Store.objects.get(name=name)


def make_user(username, role=None, store_name=None, **extra):
    user = get_user_model().objects.create_user(username, password=PASSWORD, **extra)
    if role:
        Profile.objects.create(user=user, role=role, store=store(store_name) if store_name else None)
    return user


def make_batch(name="Lotto 1", expected_count=0, store_name="Milanofiori"):
    return Batch.objects.create(name=name, expected_count=expected_count, store=store(store_name))


def create_package(weight, batch=None, notes="", sold_price=None, sold_at=None, store_name=None, photo=True, **extra):
    """Pacco di prova. Senza altre indicazioni appartiene al centro del lotto, altrimenti a Milanofiori."""
    centro = batch.store if batch else store(store_name or "Milanofiori")
    package = Package.objects.create(
        store=centro,
        weight_kg=Decimal(str(weight)),
        batch=batch,
        notes=notes,
        photo=make_image() if photo else "",
        thumb=make_image((100, 100)) if photo else "",
        **extra,
    )
    if sold_price is not None:
        package.sold_price = Decimal(str(sold_price))
        package.sold_at = sold_at or timezone.now()
        package.save()
    return package


def sell_on(day, weight, price, store_name="Milanofiori", payment="cash", premium=False, list_price=None):
    """Pacco già venduto in un giorno preciso (a mezzogiorno, ora locale)."""
    return create_package(
        weight,
        store_name=store_name,
        photo=False,
        sold_price=price,
        sold_at=start_of_day(day) + timedelta(hours=12),
        premium=premium,
        list_price=None if list_price is None else Decimal(str(list_price)),
        payment=payment,
    )


def sale_data(package, price=None, payment="cash"):
    """Ciò che invia il browser vendendo un pacco dal magazzino: il prezzo arriva già precompilato dal listino."""
    if price is None:
        price = str(package.price_with(ShopSettings.load())).replace(".", ",")
    return {"sold_price": str(price), "payment": payment}


def close_day(day, total, store_name="Milanofiori"):
    return DayClose.objects.create(store=store(store_name), date=day, receipts_total=Decimal(str(total)))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class BaseCase(TestCase):
    """Di base si entra come store manager di Milanofiori; `as_owner()` passa a un titolare."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("marco", Profile.Role.MANAGER, "Milanofiori")
        cls.owner = make_user("tom", Profile.Role.OWNER)

    def setUp(self):
        self.client.force_login(self.user)

    def as_owner(self):
        self.client.force_login(self.owner)
