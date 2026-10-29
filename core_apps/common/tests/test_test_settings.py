import pytest
from django.conf import settings
from django.db import connection


@pytest.mark.django_db
def test_automated_tests_use_safe_local_configuration():
    """Tests must not require Docker services or live payment credentials."""
    database = settings.DATABASES["default"]
    if settings.SETTINGS_MODULE == "config.settings.test_postgres":
        # The disposable PostgreSQL service of docker/test-postgres.yml, never a configured database.
        assert (connection.vendor, database["HOST"]) == ("postgresql", "test-postgres")
    else:
        assert (database["ENGINE"], connection.vendor) == ("django.db.backends.sqlite3", "sqlite")
    assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.STRIPE_SECRET_KEY == ""
    assert settings.STRIPE_WEBHOOK_SECRET == ""
    assert settings.ACCOUNTING_BASE_CURRENCY == "EUR"


def test_tests_cannot_reach_the_network():
    import socket

    with socket.socket() as sock, pytest.raises(RuntimeError, match="network connections"):
        sock.connect(("93.184.215.14", 443))


def test_deployment_dotenv_is_not_loaded_in_tests():
    assert settings.TESTING is True
