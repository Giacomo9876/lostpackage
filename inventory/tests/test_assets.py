"""CSS e JS con l'impronta del contenuto: dopo un aggiornamento il browser prende sempre i file nuovi."""
import re
import shutil
import tempfile
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from ..templatetags import assets
from .helpers import BaseCase


class FingerprintTests(SimpleTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pacchi-static-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        (self.folder / "prova.css").write_text("body { color: red; }")

    def test_the_fingerprint_follows_the_content(self):
        with override_settings(STATICFILES_DIRS=[self.folder], DEBUG=True):
            first = assets.fingerprint("prova.css")
            self.assertRegex(first, r"^[0-9a-f]{12}$")
            self.assertEqual(assets.fingerprint("prova.css"), first)
            (self.folder / "prova.css").write_text("body { color: blue; }")
            self.assertNotEqual(assets.fingerprint("prova.css"), first)

    def test_the_tag_adds_the_fingerprint_to_the_static_address(self):
        with override_settings(STATICFILES_DIRS=[self.folder], DEBUG=True):
            html = Template("{% load assets %}{% asset 'prova.css' %}").render(Context())
            self.assertEqual(html, f"/static/prova.css?v={assets.fingerprint('prova.css')}")

    def test_a_missing_file_keeps_the_plain_address(self):
        with override_settings(STATICFILES_DIRS=[self.folder], DEBUG=True):
            self.assertEqual(Template("{% load assets %}{% asset 'non-esiste.css' %}").render(Context()), "/static/non-esiste.css")

    def test_in_production_the_collected_file_is_the_one_measured(self):
        collected = Path(tempfile.mkdtemp(prefix="pacchi-collected-"))
        self.addCleanup(shutil.rmtree, collected, ignore_errors=True)
        (collected / "prova.css").write_text("versione raccolta")
        with override_settings(STATICFILES_DIRS=[self.folder], STATIC_ROOT=collected, DEBUG=False):
            measured = assets.fingerprint("prova.css")
        with override_settings(STATICFILES_DIRS=[self.folder], DEBUG=True):
            source = assets.fingerprint("prova.css")
        self.assertNotEqual(measured, source)


class PagesUseFingerprintsTests(BaseCase):
    def test_the_stylesheet_and_scripts_carry_a_fingerprint(self):
        pages = {
            reverse("inventory:dashboard"): "css/app.css",
            reverse("inventory:closing"): "js/closing.js",
            reverse("inventory:close_day"): "js/receipt-form.js",
            reverse("inventory:create"): "js/package-form.js",
        }
        for url, path in pages.items():
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertRegex(html, re.escape(f"/static/{path}?v=") + r"[0-9a-f]{12}")
