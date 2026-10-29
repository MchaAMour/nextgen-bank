import logging

from celery import shared_task
from django.conf import settings

from .stripe_reconciliation import sync_balance_transactions

logger = logging.getLogger(__name__)
SYNC_TASK = "sync_stripe_balance_transactions"


@shared_task(name=SYNC_TASK)
def sync_stripe_balance_transactions():
    """Scheduled by `manage.py schedule_stripe_sync`; skipped while no Stripe key is configured."""
    if not settings.STRIPE_SECRET_KEY:
        logger.warning("Stripe balance sync skipped: STRIPE_SECRET_KEY is not configured.")
        return {"skipped": "STRIPE_SECRET_KEY is not configured"}
    return sync_balance_transactions()
