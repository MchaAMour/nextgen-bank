import hashlib
import hmac
import json
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.db import IntegrityError, transaction
from django.db.models import Sum
from rest_framework.test import APIClient

from core_apps.accounting.models import (CustomerPayment, Invoice, JournalEntry, JournalLine, StripeCheckoutSession,
    StripeWebhookEvent)
from core_apps.accounting.services import receive_invoice_payment, record_customer_payment, post_invoice
from .test_api import client, user
from .test_ar_workflow import invoice
from .test_ar_write_api import grant

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"
WEBHOOK = BASE + "stripe/webhook/"
SECRET = "whsec_unit_test"


@pytest.fixture(autouse=True)
def stripe_settings(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_unit"
    settings.STRIPE_WEBHOOK_SECRET = SECRET


@pytest.fixture
def fake_stripe():
    """Stands in for StripeClient; records calls and hands out sequential session ids."""
    client = MagicMock()
    counter = iter(range(1, 100))

    def create(params, options=None):
        number = next(counter)
        return SimpleNamespace(id=f"cs_test_{number}", url=f"https://checkout.stripe.com/c/pay/cs_test_{number}",
                               expires_at=int(time.time()) + 86400)

    client.v1.checkout.sessions.create.side_effect = create
    with patch("core_apps.accounting.stripe_payments.stripe_client", return_value=client):
        yield client


@pytest.fixture
def posted(invoice):
    return post_invoice(invoice)


@pytest.fixture
def checkout(posted, fake_stripe):
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    return create_invoice_checkout(posted)


def signed_post(payload, secret=SECRET, signature=None):
    body = json.dumps(payload)
    timestamp = int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    header = signature if signature is not None else f"t={timestamp},v1={digest}"
    return APIClient().post(WEBHOOK, body, content_type="application/json", HTTP_STRIPE_SIGNATURE=header)


def session_event(session, event_type="checkout.session.completed", event_id="evt_1", payment_status="paid",
                  amount_total=None, currency="eur", created=None):
    amount = amount_total if amount_total is not None else int(session.amount * 100)
    return {"id": event_id, "type": event_type, "livemode": False,
            "created": created or int(time.mktime(date(2026, 1, 20).timetuple())),
            "data": {"object": {"id": session.stripe_session_id, "object": "checkout.session",
                                "payment_status": payment_status, "amount_total": amount, "currency": currency,
                                "payment_intent": "pi_test_1"}}}


def ledger_balanced():
    totals = JournalLine.objects.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return totals["debit"] == totals["credit"]


def test_checkout_uses_open_balance_and_stripe_best_practices(posted, fake_stripe):
    from core_apps.accounting.stripe_payments import INTEGRATION_IDENTIFIER, create_invoice_checkout
    receive_invoice_payment(posted, amount="30", reference="MANUAL", received_on=date(2026, 1, 2))
    session = create_invoice_checkout(posted)
    params, = fake_stripe.v1.checkout.sessions.create.call_args.args
    options = fake_stripe.v1.checkout.sessions.create.call_args.kwargs["options"]
    assert session.amount == Decimal("70.00") and session.status == "open"
    assert params["line_items"][0]["price_data"]["unit_amount"] == 7000
    assert params["line_items"][0]["price_data"]["currency"] == "eur"
    assert params["mode"] == "payment" and params["client_reference_id"] == str(posted.pk)
    assert params["integration_identifier"] == INTEGRATION_IDENTIFIER
    assert "payment_method_types" not in params
    assert options == {"idempotency_key": f"invoice-checkout-{posted.pk}-1"}


def test_checkout_reuses_session_until_balance_changes(posted, fake_stripe):
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    first = create_invoice_checkout(posted)
    assert create_invoice_checkout(posted).pk == first.pk
    receive_invoice_payment(posted, amount="10", reference="MANUAL", received_on=date(2026, 1, 2))
    second = create_invoice_checkout(posted)
    fake_stripe.v1.checkout.sessions.expire.assert_called_once_with(first.stripe_session_id)
    first.refresh_from_db()
    assert (first.status, second.amount, second.status) == ("expired", Decimal("90.00"), "open")
    assert fake_stripe.v1.checkout.sessions.create.call_count == 2


def test_completed_session_blocks_a_replacement(posted, fake_stripe, checkout):
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    receive_invoice_payment(posted, amount="10", reference="MANUAL", received_on=date(2026, 1, 2))
    fake_stripe.v1.checkout.sessions.expire.side_effect = stripe.InvalidRequestError("complete", None)
    fake_stripe.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(status="complete")
    with pytest.raises(Exception, match="completed"):
        create_invoice_checkout(posted)
    assert StripeCheckoutSession.objects.get().status == "open"


def test_session_already_expired_at_stripe_is_replaced(posted, fake_stripe, checkout):
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    receive_invoice_payment(posted, amount="10", reference="MANUAL", received_on=date(2026, 1, 2))
    fake_stripe.v1.checkout.sessions.expire.side_effect = stripe.InvalidRequestError("expired", None)
    fake_stripe.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(status="expired")
    assert create_invoice_checkout(posted).amount == Decimal("90.00")


@pytest.mark.parametrize("state", ["draft", "paid"])
def test_checkout_requires_open_balance(invoice, fake_stripe, state):
    from django.core.exceptions import ValidationError
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    if state == "paid":
        receive_invoice_payment(post_invoice(invoice), amount="100", reference="FULL", received_on=date(2026, 1, 2))
    with pytest.raises(ValidationError):
        create_invoice_checkout(invoice)
    fake_stripe.v1.checkout.sessions.create.assert_not_called()


def test_one_active_session_per_invoice_in_the_database(checkout):
    with pytest.raises(IntegrityError), transaction.atomic():
        StripeCheckoutSession.objects.create(invoice=checkout.invoice, stripe_session_id="cs_other", url=checkout.url,
            amount=checkout.amount, currency="EUR", expires_at=checkout.expires_at)


def test_checkout_api(client, user, posted, fake_stripe):
    url = f"{BASE}invoices/{posted.pk}/checkout/"
    assert client.post(url).status_code == 403
    grant(user, "add_stripecheckoutsession")
    user = type(user).objects.get(pk=user.pk)
    client.force_authenticate(user)
    response = client.post(url)
    assert response.status_code == 201
    assert (response.data["amount"], response.data["url"]) == ("100.00", "https://checkout.stripe.com/c/pay/cs_test_1")
    assert StripeCheckoutSession.objects.get().created_by == user
    fake_stripe.v1.checkout.sessions.expire.side_effect = None
    fake_stripe.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError("network down")
    receive_invoice_payment(posted, amount="1", reference="MANUAL", received_on=date(2026, 1, 2))
    failed = client.post(url)
    assert failed.status_code == 502 and "network" not in str(failed.data)
    assert StripeCheckoutSession.objects.get().status == "open"  # The failed replacement rolled back.


@pytest.mark.parametrize("signature", ["", "t=1,v1=bad", None])
def test_webhook_rejects_unsigned_or_forged_events(checkout, signature):
    payload = session_event(checkout)
    response = signed_post(payload, secret="whsec_wrong") if signature is None else signed_post(payload,
                                                                                                signature=signature)
    assert response.status_code == 400
    assert not StripeWebhookEvent.objects.exists() and not CustomerPayment.objects.exists()


def test_webhook_unconfigured_secret_returns_503(settings, checkout):
    settings.STRIPE_WEBHOOK_SECRET = ""
    assert signed_post(session_event(checkout)).status_code == 503


def test_paid_session_posts_clearing_payment_once(checkout):
    response = signed_post(session_event(checkout))
    assert (response.status_code, response.data["status"]) == (200, "processed")
    checkout.refresh_from_db()
    payment = checkout.payment
    assert (checkout.status, checkout.payment_intent_id) == ("paid", "pi_test_1")
    assert (payment.reference, payment.amount, payment.received_on) == ("stripe:pi_test_1", 100, date(2026, 1, 20))
    lines = {line.account.code: (line.debit, line.credit) for line in payment.journal_entry.lines.all()}
    assert lines == {"1010": (Decimal("100"), 0), "1100": (0, Decimal("100"))}
    assert Invoice.objects.with_balances().get(pk=checkout.invoice_id).open_balance == 0
    entries = JournalEntry.objects.count()
    assert signed_post(session_event(checkout)).data["status"] == "processed"
    later = session_event(checkout, "checkout.session.async_payment_succeeded", event_id="evt_2")
    assert signed_post(later).status_code == 200
    assert (JournalEntry.objects.count(), CustomerPayment.objects.count()) == (entries, 1)
    assert StripeWebhookEvent.objects.get(event_id="evt_1").attempts == 1
    assert ledger_balanced()


def test_async_payment_waits_for_success(checkout):
    signed_post(session_event(checkout, payment_status="unpaid"))
    checkout.refresh_from_db()
    assert checkout.status == "awaiting_payment" and not CustomerPayment.objects.exists()
    signed_post(session_event(checkout, "checkout.session.async_payment_succeeded", event_id="evt_2"))
    checkout.refresh_from_db()
    assert checkout.status == "paid" and CustomerPayment.objects.count() == 1


def test_async_payment_failure_and_expiry_post_nothing(posted, fake_stripe, checkout):
    signed_post(session_event(checkout, payment_status="unpaid"))
    signed_post(session_event(checkout, "checkout.session.async_payment_failed", event_id="evt_2"))
    checkout.refresh_from_db()
    assert checkout.status == "failed"
    from core_apps.accounting.stripe_payments import create_invoice_checkout
    retry = create_invoice_checkout(posted)
    signed_post(session_event(retry, "checkout.session.expired", event_id="evt_3", payment_status="unpaid"))
    retry.refresh_from_db()
    assert retry.status == "expired"
    assert not CustomerPayment.objects.exists() and JournalEntry.objects.count() == 1


def test_payment_beyond_open_balance_becomes_customer_credit(posted, checkout):
    receive_invoice_payment(posted, amount="40", reference="MANUAL", received_on=date(2026, 1, 2))
    signed_post(session_event(checkout))  # Stripe collected the full 100 shown at checkout.
    payment = CustomerPayment.objects.with_balances().get(reference="stripe:pi_test_1")
    assert (payment.amount, payment.allocated_total, payment.unapplied_amount) == (100, 60, 40)
    assert Invoice.objects.with_balances().get(pk=posted.pk).open_balance == 0
    assert ledger_balanced()


def test_unknown_sessions_and_other_events_are_ignored(checkout):
    stranger = session_event(SimpleNamespace(stripe_session_id="cs_not_ours", amount=Decimal("5")), event_id="evt_x")
    assert signed_post(stranger).data["status"] == "ignored"
    other = {"id": "evt_y", "type": "customer.created", "livemode": False, "created": 1, "data": {"object": {}}}
    assert signed_post(other).data["status"] == "ignored"
    assert StripeWebhookEvent.objects.filter(status="ignored").count() == 2
    assert not CustomerPayment.objects.exists()


def test_currency_mismatch_fails_and_retry_succeeds(checkout):
    response = signed_post(session_event(checkout, currency="usd"))
    assert response.status_code == 500
    event = StripeWebhookEvent.objects.get()
    assert (event.status, event.attempts) == ("failed", 1) and "currency" in event.error
    assert not CustomerPayment.objects.exists()
    with patch("core_apps.accounting.stripe_payments._apply", return_value=True):
        assert signed_post(session_event(checkout, currency="usd")).status_code == 200
    event.refresh_from_db()
    assert (event.status, event.attempts, event.error) == ("processed", 2, "")


def test_payment_trigger_accepts_only_cash_or_clearing_debits(posted):
    from django.core.exceptions import ValidationError
    payment = record_customer_payment(posted.customer, amount="5", currency="EUR", reference="CLR",
                                      received_on=date(2026, 1, 2), cash_account="1010")
    assert payment.journal_entry.lines.filter(account__code="1010", debit=5).exists()
    with pytest.raises(ValidationError):
        record_customer_payment(posted.customer, amount="5", currency="EUR", reference="EXP",
                                received_on=date(2026, 1, 2), cash_account="5000")
