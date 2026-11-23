"""Foto dello scontrino dalla fotocamera o dalla galleria del telefono.

Il modulo ha due campi file con lo stesso nome «photo»: uno con «capture» (apre la fotocamera) e uno senza
(apre la galleria). Il browser li invia tutti e due; quello rimasto vuoto arriva senza nome di file e
Django lo scarta, quindi al server arriva sempre una sola foto, da qualunque pulsante venga.
"""
import re
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import DayClose, Franchise, Profile
from .helpers import TEMP_MEDIA, make_image, make_user

CLOSE = reverse("inventory:close_day")
FRANCHISE_DAY = reverse("inventory:franchise_day")
BOUNDARY = "----confine-di-prova"


def multipart(fields, files):
    """Il corpo di un modulo come lo manda il browser: `files` è una lista di (nome, nome del file, byte)."""
    parts = []
    for name, value in fields.items():
        parts.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, content in files:
        head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        kind = "image/jpeg" if content else "application/octet-stream"
        parts.append(head.encode() + f"Content-Type: {kind}\r\n\r\n".encode() + content + b"\r\n")
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class GalleryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = make_user("marco", Profile.Role.MANAGER, "Milanofiori")

    def setUp(self):
        self.client.force_login(self.manager)

    def photo_inputs(self, html):
        return re.findall(r'<input type="file"[^>]*name="photo"[^>]*>', html)

    def test_the_form_offers_camera_and_gallery(self):
        html = self.client.get(CLOSE).content.decode()
        camera, gallery = self.photo_inputs(html)
        self.assertIn('capture="environment"', camera)
        self.assertNotIn("capture", gallery)
        for tag in (camera, gallery):
            self.assertIn('accept="image/*"', tag)
            self.assertIn("data-photo-input", tag)
        self.assertIn("Scegli dalla galleria", html)
        self.assertIn('for="id_photo_gallery"', html)

    def test_the_franchise_form_offers_both_too(self):
        napoli = Franchise.objects.create(name="Punto Napoli", city="Napoli")
        anna = make_user("anna")
        Profile.objects.create(user=anna, role=Profile.Role.FRANCHISE, franchise=napoli)
        self.client.force_login(anna)
        html = self.client.get(FRANCHISE_DAY).content.decode()
        self.assertEqual(len(self.photo_inputs(html)), 2)
        self.assertIn("Scegli dalla galleria", html)

    def post(self, files):
        body = multipart({"receipts_total": "100"}, files)
        return self.client.generic("POST", CLOSE, body, content_type=f"multipart/form-data; boundary={BOUNDARY}")

    def test_a_photo_from_the_gallery_is_saved(self):
        # fotocamera vuota, galleria con la foto: come la invia il telefono
        response = self.post([("photo", "", b""), ("photo", "IMG_2041.jpg", make_image().read())])
        self.assertEqual(response.status_code, 302)
        close = DayClose.objects.get()
        self.assertTrue(close.has_photo)
        self.assertEqual(close.receipts_total, D("100.00"))
        self.assertNotIn("IMG_2041", close.photo.name)  # ricodificata con un nome casuale, come sempre

    def test_a_photo_from_the_camera_still_works(self):
        response = self.post([("photo", "image.jpg", make_image().read()), ("photo", "", b"")])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DayClose.objects.get().has_photo)

    def test_no_photo_from_either_is_fine(self):
        response = self.post([("photo", "", b""), ("photo", "", b"")])
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DayClose.objects.get().has_photo)

    def test_a_bad_file_from_the_gallery_is_refused(self):
        response = self.post([("photo", "", b""), ("photo", "nota.jpg", b"non sono una foto")])
        self.assertEqual(response.status_code, 200)
        self.assertIn("photo", response.context["form"].errors)
        self.assertFalse(DayClose.objects.exists())
