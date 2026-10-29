"""Test settings against PostgreSQL, which SQLite cannot stand in for: row locks, triggers in PL/pgSQL
and exact decimal aggregation (PG-01). Used by docker/test-postgres.yml and `make test-postgres`."""

from os import getenv

from .test import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": getenv("TEST_POSTGRES_DB", "nextgen_test"),
        "USER": getenv("TEST_POSTGRES_USER", "postgres"),
        "PASSWORD": getenv("TEST_POSTGRES_PASSWORD", "postgres"),
        "HOST": getenv("TEST_POSTGRES_HOST", "test-postgres"),
        "PORT": getenv("TEST_POSTGRES_PORT", "5432"),
    }
}
