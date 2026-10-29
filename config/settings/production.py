from os import getenv, path
from dotenv import load_dotenv
from .base import *  # noqa
from .base import BASE_DIR, MIDDLEWARE


prod_env_file = path.join(BASE_DIR, ".envs", ".env.production")

if path.isfile(prod_env_file):
    load_dotenv(prod_env_file)


def env_flag(name, default=False):
    """Environment strings are always truthy; only 'true', '1' or 'yes' turn a flag on."""
    value = getenv(name)
    return default if value is None else value.strip().lower() in {"true", "1", "yes"}


SECRET_KEY = getenv("SECRET_KEY")

DEBUG = env_flag("DEBUG")

SITE_NAME = getenv("SITE_NAME")

ADMINS = [("Admin", getenv("ADMIN_EMAIL", ""))]

# The public host name, e.g. next-gen-bank.srv1915831.hstgr.cloud; TLS ends at the edge proxy (Caddy).
DOMAIN = getenv("DOMAIN")

ALLOWED_HOSTS = [DOMAIN]

ADMIN_URL = getenv("ADMIN_URL")

EMAIL_BACKEND = "djcelery_email.backends.CeleryEmailBackend"
EMAIL_HOST = getenv("EMAIL_HOST")
EMAIL_HOST_USER = getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_PORT = getenv("EMAIL_PORT")
EMAIL_USE_TLS = env_flag("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = getenv("DEFAULT_FROM_EMAIL")
ADMIN_EMAIL = getenv("ADMIN_EMAIL")

MAX_UPLOAD_SIZE = 1 * 1024 * 1024

# There is no web server in front of gunicorn besides the edge Caddy, so WhiteNoise
# serves the collected static files: compressed, with hashed names cached for a year.
MIDDLEWARE = MIDDLEWARE[:1] + ["whitenoise.middleware.WhiteNoiseMiddleware"] + MIDDLEWARE[1:]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"]

LOCKOUT_DURATION = timedelta(minutes=10)

LOGIN_ATTEMPTS = 3

OTP_EXPIRATION = timedelta(minutes=1)

# Caddy terminates TLS and sets X-Forwarded-Proto, so Django knows the request arrived over HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env_flag("SECURE_SSL_REDIRECT", default=True)

SESSION_COOKIE_SECURE = True

CSRF_COOKIE_SECURE = True

COOKIE_SECURE = True

SECURE_HSTS_SECONDS = 300

# Off by default: the parent domain belongs to the hosting provider.
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_flag("SECURE_HSTS_INCLUDE_SUBDOMAINS")

SECURE_HSTS_PRELOAD = env_flag("SECURE_HSTS_PRELOAD")

SECURE_CONTENT_TYPE_NOSNIFF = env_flag("SECURE_CONTENT_TYPE_NOSNIFF", default=True)
