"""Impostazioni del gestionale Pacchi Perduti.

Tutto ciò che cambia tra sviluppo e produzione arriva da variabili d'ambiente
(o da un file .env). L'app non parte senza una SECRET_KEY, e in produzione
rifiuta la chiave d'esempio: meglio un errore chiaro che un sito esposto
con una chiave nota.
"""
import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Manca DJANGO_SECRET_KEY. In sviluppo copia .env.example in .env; "
        "in produzione imposta la variabile d'ambiente."
    )
if not DEBUG and SECRET_KEY.startswith("sviluppo-"):
    raise ImproperlyConfigured("In produzione serve una DJANGO_SECRET_KEY vera, non quella d'esempio.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Indirizzo del pannello admin (con slash finale). Cambialo in produzione.
ADMIN_URL = os.environ.get("DJANGO_ADMIN_URL", "admin/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "inventory.apps.InventoryConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Default-deny: ogni vista richiede il login, salvo quelle marcate
    # con @login_not_required (login e login dell'admin).
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Ultimo: risponde con la pagina di blocco quando ci sono troppi accessi falliti.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# SQLite di default; con DATABASE_URL si passa a PostgreSQL senza toccare il codice.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Blocco dei tentativi di accesso (django-axes): dopo AXES_FAILURE_LIMIT password
# sbagliate per lo stesso nome utente, quell'utente resta bloccato per
# AXES_COOLOFF_MINUTES minuti, anche con la password giusta. Il conteggio si
# azzera a ogni accesso riuscito. Si blocca per nome utente e non per indirizzo IP
# di proposito: dietro un proxy (PythonAnywhere, nginx) l'IP visto dall'app può
# essere quello del proxy, e bloccherebbe tutti insieme.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = int(os.environ.get("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_MINUTES = int(os.environ.get("AXES_COOLOFF_MINUTES", "15"))
AXES_COOLOFF_TIME = timedelta(minutes=AXES_COOLOFF_MINUTES)
AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_RESET_ON_SUCCESS = True
AXES_HTTP_RESPONSE_CODE = 429
AXES_LOCKOUT_TEMPLATE = "registration/locked.html"
AXES_ENABLED = env_bool("AXES_ENABLED", True)
# Il controllo di axes avverte che senza 'ip_address' si può aggirare il limite cambiando
# User-Agent o cookie: vale solo se questi sono tra i parametri di blocco, e qui non lo sono.
SILENCED_SYSTEM_CHECKS = ["axes.W006"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "inventory:list"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Le foto NON sono pubbliche: MEDIA_URL non è servito da nessuna parte,
# le immagini passano da una vista che richiede il login (inventory.views.package_image).
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
FILE_UPLOAD_PERMISSIONS = 0o640
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "15"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 0 if DEBUG else 3600

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_REFERRER_POLICY = "same-origin"
    if env_bool("DJANGO_BEHIND_PROXY"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
