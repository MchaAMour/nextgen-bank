from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django_celery_beat.models import PeriodicTask

from core_apps.accounting.tasks import sync_stripe_balance_transactions

pytestmark = pytest.mark.django_db


def test_schedule_command_creates_and_updates_one_task():
    call_command("schedule_stripe_sync")
    call_command("schedule_stripe_sync", "--every-minutes", "30")
    task = PeriodicTask.objects.get(task="sync_stripe_balance_transactions")
    assert (task.enabled, task.interval.every, task.interval.period) == (True, 30, "minutes")
    call_command("schedule_stripe_sync", "--disable")
    assert PeriodicTask.objects.get(pk=task.pk).enabled is False
    with pytest.raises(CommandError):
        call_command("schedule_stripe_sync", "--every-minutes", "1")


def test_task_is_skipped_without_a_stripe_key(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("core_apps.accounting.tasks.sync_balance_transactions") as sync:
        assert "skipped" in sync_stripe_balance_transactions.delay().get()
    sync.assert_not_called()
    settings.STRIPE_SECRET_KEY = "sk_test_unit"
    with patch("core_apps.accounting.tasks.sync_balance_transactions", return_value={"listed": 0}), \
            patch("core_apps.accounting.tasks.recover_checkout_sessions", return_value={}):
        assert sync_stripe_balance_transactions.delay().get() == {"listed": 0, "recovered_sessions": {}}
