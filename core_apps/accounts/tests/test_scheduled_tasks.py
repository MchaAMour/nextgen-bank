from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from celery import current_app
from celery.schedules import crontab
from django.conf import settings
from django.utils import timezone

from core_apps.accounts.models import Transaction
from core_apps.accounts.tasks import detect_suspicious_activities
from core_apps.accounting.tests.test_api import user

pytestmark = pytest.mark.django_db


def test_beat_schedule_names_registered_tasks():
    import config.celery_app  # noqa: F401  Registers the tasks with the app.

    current_app.loader.import_default_modules()
    for entry in settings.CELERY_BEAT_SCHEDULE.values():
        assert entry["task"] in current_app.tasks
        assert isinstance(entry["schedule"], crontab)


def test_suspicious_activity_sends_one_alert_for_recent_large_transactions(user):
    def transaction(amount, age):
        created = Transaction.objects.create(user=user, amount=Decimal(amount), description="t",
                                             transaction_type=Transaction.TransactionType.DEPOSIT)
        Transaction.objects.filter(pk=created.pk).update(created_at=timezone.now() - age)

    transaction("20000", timedelta(minutes=10))
    transaction("30000", timedelta(hours=5))  # Outside the one-hour window: reported by an earlier run.
    with mock.patch("core_apps.accounts.tasks.send_suspicious_activity_alert", return_value=1) as alert:
        detect_suspicious_activities()
    (activities,), _ = alert.call_args
    assert alert.call_count == 1
    assert activities == ["Large transaction detected: 20000.00 by user reader@example.com"]
