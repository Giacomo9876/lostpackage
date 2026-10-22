"""Indirizzi dei file statici con l'impronta del contenuto: `{% asset 'css/app.css' %}` → `/static/css/app.css?v=…`.

CSS e JS restano in cache nel browser (WhiteNoise li serve con un max-age), ma dopo un aggiornamento devono
arrivare quelli nuovi: l'impronta cambia quando cambia il file, quindi cambia l'indirizzo e il browser lo
riscarica da solo. Nessun nome di file da cambiare a mano e nessun rischio se si dimentica qualcosa: se il file
non si trova, l'indirizzo resta quello normale.
"""
import hashlib
from functools import lru_cache

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.templatetags.static import static

register = template.Library()


def _served_file(path):
    """Il file che arriva davvero al browser: in produzione quello raccolto da collectstatic, in sviluppo l'originale."""
    if not settings.DEBUG:
        try:
            if staticfiles_storage.exists(path):
                return staticfiles_storage.path(path)
        except (NotImplementedError, OSError):
            pass
    return finders.find(path)


def fingerprint(path):
    found = _served_file(path)
    if not found:
        return ""
    digest = hashlib.sha256()
    with open(found, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()[:12]


# In produzione i file cambiano solo con un aggiornamento, che riavvia l'app: l'impronta si calcola una volta.
_cached_fingerprint = lru_cache(maxsize=64)(fingerprint)


@register.simple_tag
def asset(path):
    version = fingerprint(path) if settings.DEBUG else _cached_fingerprint(path)
    url = static(path)
    return f"{url}?v={version}" if version else url
