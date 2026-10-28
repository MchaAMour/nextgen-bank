"""Settings for automated tests without Docker services or secret values."""

from .base import *  # noqa: F403


SECRET_KEY = "test-secret-key"
DEBUG = False
USE_TZ = True
ADMIN_URL = "admin/"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "test@example.com"
ADMIN_EMAIL = "admin@example.com"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
COOKIE_SECURE = False
STRIPE_SECRET_KEY = ""
STRIPE_PUBLISHABLE_KEY = ""
STRIPE_WEBHOOK_SECRET = ""
ACCOUNTING_BASE_CURRENCY = "USD"
