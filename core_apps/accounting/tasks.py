import logging

from celery import shared_task
from django.conf import settings

from .stripe_payments import recover_checkout_sessions
from .stripe_reconciliation import sync_balance_transactions

logger = logging.getLogger(__name__)
SYNC_TASK = "sync_stripe_balance_transactions"


@shared_task(name=SYNC_TASK)
def sync_stripe_balance_transactions():
    """Scheduled by `manage.py schedule_stripe_sync`; skipped while no Stripe key is configured.

    Settles checkout sessions whose webhook never arrived first, so their charges find
    the payment already posted to Stripe clearing.
    """
    if not settings.STRIPE_SECRET_KEY:
        logger.warning("Stripe balance sync skipped: STRIPE_SECRET_KEY is not configured.")
        return {"skipped": "STRIPE_SECRET_KEY is not configured"}
    recovered = recover_checkout_sessions()
    return {**sync_balance_transactions(), "recovered_sessions": recovered}
