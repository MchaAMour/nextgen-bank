import pytest
from django.conf import settings
from django.db import connection


@pytest.mark.django_db
def test_automated_tests_use_safe_local_configuration():
    """Tests must not require Docker services or live payment credentials."""
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert connection.vendor == "sqlite"
    assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.STRIPE_SECRET_KEY == ""
    assert settings.STRIPE_WEBHOOK_SECRET == ""
    assert settings.ACCOUNTING_BASE_CURRENCY == "USD"
