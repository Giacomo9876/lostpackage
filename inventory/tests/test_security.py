"""Protezioni generali: richieste troppo grandi, Content-Security-Policy, header di sicurezza, registro."""
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from ..access import Scope
from ..models import Profile
from ..security import RequestSizeLimitMiddleware
from ..supply_views import SuppliesView
from .helpers import make_user, store


class RequestSizeTests(TestCase):
    def big_post(self, url, megabytes):
        size = str(megabytes * 1024 * 1024)
        return self.client.generic("POST", url, b"", content_type="multipart/form-data; boundary=x", CONTENT_LENGTH=size)

    def test_an_oversized_request_is_refused_before_being_read_even_without_login(self):
        for url in (reverse("login"), reverse("inventory:create"), reverse("inventory:receipt_read"), "/qualsiasi/"):
            with self.subTest(url=url):
                response = self.big_post(url, settings.MAX_UPLOAD_MB + 2)
                self.assertEqual(response.status_code, 413)
                self.assertIn("troppo grande", response.content.decode())

    def test_requests_within_the_limit_go_through(self):
        response = self.big_post(reverse("login"), settings.MAX_UPLOAD_MB)
        self.assertNotEqual(response.status_code, 413)

    def test_a_bad_length_header_is_not_an_error(self):
        factory = RequestFactory()
        request = factory.get("/")
        request.META["CONTENT_LENGTH"] = "non-un-numero"
        middleware = RequestSizeLimitMiddleware(lambda r: "passata")
        self.assertEqual(middleware(request), "passata")

    @override_settings(MAX_UPLOAD_MB=1)
    def test_the_limit_follows_the_upload_setting(self):
        self.assertEqual(self.big_post(reverse("login"), 3).status_code, 413)


class HeadersTests(TestCase):
    def test_every_page_has_the_content_security_policy(self):
        user = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        pages = [reverse("login"), f"/{settings.ADMIN_URL}login/"]
        for url in pages:
            with self.subTest(url=url):
                policy = self.client.get(url)["Content-Security-Policy"]
                for part in ("default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'"):
                    self.assertIn(part, policy)
                self.assertNotIn("unsafe-eval", policy)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        self.client.force_login(user)
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertIn("script-src 'self'", response["Content-Security-Policy"])

    def test_the_usual_django_headers_are_there(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")

    @override_settings(CONTENT_SECURITY_POLICY="")
    def test_the_policy_can_be_switched_off(self):
        self.assertNotIn("Content-Security-Policy", self.client.get(reverse("login")))

    def test_pages_have_no_inline_scripts(self):
        """La policy vieta gli script inline: le pagine non devono averne (solo <script src=…>)."""
        self.client.force_login(make_user("tom", Profile.Role.OWNER))
        for name in ("inventory:dashboard", "inventory:closing", "inventory:create", "inventory:receipts", "inventory:supplies"):
            with self.subTest(page=name):
                url = reverse(name)
                if name in ("inventory:closing", "inventory:create"):
                    url += f"?centro={store().pk}"
                html = self.client.get(url).content.decode()
                for tag in html.split("<script")[1:]:
                    self.assertIn("src=", tag.split(">")[0])
                self.assertNotRegex(html, r"\son(click|load|submit|change|input)=")


class TemplateCommentTests(SimpleTestCase):
    def test_template_comments_stay_on_one_line(self):
        """{# … #} vale solo su una riga: spezzato, Django lo stampa nella pagina (per più righe c'è {% comment %})."""
        root = Path(settings.BASE_DIR)
        broken = [
            f"{path.relative_to(root)}:{number}"
            for path in sorted(root.glob("**/templates/**/*.html"))
            if "venv" not in path.parts
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.count("{#") != line.count("#}")
        ]
        self.assertEqual(broken, [])


class LoggingTests(SimpleTestCase):
    def test_security_warnings_and_server_errors_reach_the_server_log(self):
        self.assertEqual(logging.getLogger("django.security").level, logging.WARNING)
        self.assertEqual(logging.getLogger("django.request").level, logging.ERROR)
        self.assertTrue(any(isinstance(h, logging.StreamHandler) for h in logging.getLogger("django.security").handlers))


class SupplyOverviewGuardTests(TestCase):
    def test_only_owners_can_build_the_overview(self):
        manager = make_user("mario", Profile.Role.MANAGER, "Milanofiori")
        request = RequestFactory().get("/risorse/")
        request.user = manager
        request.scope = Scope.for_user(manager)
        view = SuppliesView()
        view.setup(request)
        with self.assertRaises(PermissionDenied):
            view.overview()
