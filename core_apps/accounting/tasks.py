from celery import shared_task

from .stripe_reconciliation import sync_balance_transactions


@shared_task(name="sync_stripe_balance_transactions")
def sync_stripe_balance_transactions():
    """Schedule through django_celery_beat (Periodic tasks in the admin)."""
    return sync_balance_transactions()
