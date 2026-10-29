from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from core_apps.accounting import stripe_payments
from core_apps.accounting.stripe_payments import stripe_client
from .test_api import client, user
from .test_ar_workflow import invoice
from .test_ar_write_api import grant

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_account(settings):
    """StripeClient whose key belongs to acct_ledger; returns the class mock."""
    settings.STRIPE_SECRET_KEY, settings.STRIPE_ACCOUNT_ID = "sk_test_unit", "acct_ledger"
    stripe_payments._verified_accounts.clear()
    with patch("core_apps.accounting.stripe_payments.stripe.StripeClient") as client_class:
        client_class.return_value.v1.accounts.retrieve_current.return_value = MagicMock(id="acct_ledger")
        yield client_class
    stripe_payments._verified_accounts.clear()


def test_client_is_verified_once_per_key(stripe_account):
    assert stripe_client() is stripe_account.return_value
    stripe_client()
    assert stripe_account.return_value.v1.accounts.retrieve_current.call_count == 1


def test_key_for_another_account_is_refused(settings, stripe_account):
    settings.STRIPE_ACCOUNT_ID = "acct_other"
    with pytest.raises(ImproperlyConfigured, match="belongs to Stripe account acct_ledger, not STRIPE_ACCOUNT_ID"):
        stripe_client()
    settings.STRIPE_ACCOUNT_ID = ""
    with pytest.raises(ImproperlyConfigured, match="STRIPE_ACCOUNT_ID is not configured"):
        stripe_client()


def test_api_reports_a_wrong_account_as_unavailable(settings, stripe_account, client, user, invoice):
    from core_apps.accounting.services import post_invoice
    settings.STRIPE_ACCOUNT_ID = "acct_other"
    assert client.get("/api/v1/accounting/stripe/reconciliation/").status_code == 503
    grant(user, "add_stripecheckoutsession")
    client.force_authenticate(type(user).objects.get(pk=user.pk))
    post_invoice(invoice)
    assert client.post(f"/api/v1/accounting/invoices/{invoice.pk}/checkout/").status_code == 503
