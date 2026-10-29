"""Stripe Checkout for AR invoices.

Sessions are created for an invoice's open balance. Payments are recorded from
verified webhook events, or by `recover_checkout_sessions`, which reads a session
the webhook missed straight from the Stripe API. Stripe clearing (1010) is debited
and AR credited, then the payment is allocated to the invoice up to its open
balance at that time.
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Invoice, StripeCheckoutSession, StripeWebhookEvent
from .services import record_customer_payment

# Tags these sessions in the Stripe Dashboard; the suffix is fixed so all sessions share one label.
INTEGRATION_IDENTIFIER = "nextgen_ar_invoice_qhzmtwkd"
MINOR_UNITS = Decimal("100")  # EUR has two decimal places.
REUSE_MARGIN = timedelta(minutes=10)
PAID_STATUSES = {"paid", "no_payment_required"}
FULFILLING_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
# (key, account) pairs already checked in this process.
_verified_accounts = set()


def stripe_client():
    """A client for STRIPE_SECRET_KEY, once its key is confirmed to belong to STRIPE_ACCOUNT_ID.

    The ledger reconciles one Stripe account; a key for any other account (a stale
    container after a key change, say) would sync that account's transactions into it.
    """
    key, expected = settings.STRIPE_SECRET_KEY, settings.STRIPE_ACCOUNT_ID
    if not key:
        raise ImproperlyConfigured("STRIPE_SECRET_KEY is not configured.")
    if not expected:
        raise ImproperlyConfigured("STRIPE_ACCOUNT_ID is not configured; set it to the account this ledger uses.")
    client = stripe.StripeClient(key)
    if (key, expected) not in _verified_accounts:
        actual = client.v1.accounts.retrieve_current().id
        if actual != expected:
            raise ImproperlyConfigured(f"STRIPE_SECRET_KEY belongs to Stripe account {actual}, "
                                       f"not STRIPE_ACCOUNT_ID {expected}.")
        _verified_accounts.add((key, expected))
    return client


def _with_query(url, **params):
    parts = urlsplit(url)
    query = urlencode(parse_qsl(parts.query) + [(key, str(value)) for key, value in params.items()])
    return urlunsplit(parts._replace(query=query))


def _from_timestamp(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


@transaction.atomic
def create_invoice_checkout(invoice, *, actor=None):
    """Return a Checkout Session for the invoice's open balance, reusing an unexpired one.

    The invoice row stays locked during the Stripe call, so concurrent requests for
    the same invoice create at most one session.
    """
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    open_balance = invoice.open_balance
    if invoice.status != "posted" or open_balance <= 0:
        raise ValidationError("Checkout requires a posted invoice with an open balance.")
    client = stripe_client()
    active = StripeCheckoutSession.objects.filter(invoice=invoice, status__in=StripeCheckoutSession.ACTIVE).first()
    if active and active.status == StripeCheckoutSession.Status.AWAITING_PAYMENT:
        raise ValidationError("A Stripe payment for this invoice is being processed.")
    if active:
        if active.amount == open_balance and active.expires_at > timezone.now() + REUSE_MARGIN:
            return active
        try:
            client.v1.checkout.sessions.expire(active.stripe_session_id)
        except stripe.InvalidRequestError:
            # Already expired (for example, by an earlier attempt that rolled back) is fine; a
            # completed session will be recorded by its webhook.
            if client.v1.checkout.sessions.retrieve(active.stripe_session_id).status != "expired":
                raise ValidationError("The previous Checkout Session was completed; wait for its webhook.")
        active.status = StripeCheckoutSession.Status.EXPIRED
        active.save(update_fields=["status", "updated_at"])
    attempt = StripeCheckoutSession.objects.filter(invoice=invoice).count() + 1
    metadata = {"invoice_id": str(invoice.pk), "invoice_number": invoice.number}
    params = {
        "mode": "payment",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": invoice.currency.lower(),
                "unit_amount": int(open_balance * MINOR_UNITS),
                "product_data": {"name": f"Invoice {invoice.number}"},
            },
        }],
        "client_reference_id": str(invoice.pk),
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
        "success_url": settings.STRIPE_CHECKOUT_SUCCESS_URL,
        # The cancel page offers this invoice's open session again.
        "cancel_url": _with_query(settings.STRIPE_CHECKOUT_CANCEL_URL, invoice=invoice.pk),
        "integration_identifier": INTEGRATION_IDENTIFIER,
    }
    if invoice.customer.email:
        params["customer_email"] = invoice.customer.email
    session = client.v1.checkout.sessions.create(
        params, options={"idempotency_key": f"invoice-checkout-{invoice.pk}-{attempt}"})
    return StripeCheckoutSession.objects.create(
        invoice=invoice, stripe_session_id=session.id, url=session.url, amount=open_balance,
        currency=invoice.currency, expires_at=_from_timestamp(session.expires_at), created_by=actor)


def verify_event(payload, signature):
    """Return the event as a dict after checking Stripe's signature; raises on invalid input.

    Verification needs only the webhook signing secret, not an API key or client.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise ImproperlyConfigured("STRIPE_WEBHOOK_SECRET is not configured.")
    stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    return json.loads(payload)


def process_event(event):
    """Apply a verified event once. Returns the stored event; status `failed` asks Stripe to retry."""
    with transaction.atomic():
        record, _ = StripeWebhookEvent.objects.select_for_update().get_or_create(
            event_id=event["id"], defaults={"event_type": event["type"], "livemode": event["livemode"],
                                            "payload": event})
        if record.status in StripeWebhookEvent.FINISHED:
            return record
        record.attempts += 1
        try:
            with transaction.atomic():
                handled = _apply(event)
        except Exception as error:  # Stored for diagnosis; the savepoint rolled back any partial work.
            record.status, record.error = StripeWebhookEvent.Status.FAILED, f"{type(error).__name__}: {error}"
        else:
            record.status = StripeWebhookEvent.Status.PROCESSED if handled else StripeWebhookEvent.Status.IGNORED
            record.error, record.processed_at = "", timezone.now()
        record.save()
        return record


def _apply(event):
    """Return False for events this integration does not act on."""
    if not event["type"].startswith("checkout.session."):
        return False
    data = event["data"]["object"]
    local = StripeCheckoutSession.objects.select_for_update().filter(stripe_session_id=data["id"]).first()
    if local is None:
        return False  # Not created by this application.
    if local.status == StripeCheckoutSession.Status.PAID:
        return True  # A paid session is final; replays and later events change nothing.
    if event["type"] in FULFILLING_EVENTS:
        if data["payment_status"] in PAID_STATUSES:
            _record_payment(local, data, received_at=event["created"])
        else:
            local.status = StripeCheckoutSession.Status.AWAITING_PAYMENT
    elif event["type"] == "checkout.session.async_payment_failed":
        local.status = StripeCheckoutSession.Status.FAILED
    elif event["type"] == "checkout.session.expired":
        local.status = StripeCheckoutSession.Status.EXPIRED
    else:
        return False
    local.payment_intent_id = data.get("payment_intent") or local.payment_intent_id
    local.save()
    return True


def _record_payment(local, data, *, received_at):
    """Record what Stripe collected and allocate it; any excess stays as customer credit."""
    currency = data["currency"].upper()
    if currency != local.currency:
        raise ValidationError(f"Session currency {currency} does not match {local.currency}.")
    amount = Decimal(data["amount_total"]) / MINOR_UNITS
    invoice = Invoice.objects.select_for_update().get(pk=local.invoice_id)
    allocated = min(amount, invoice.open_balance) if invoice.status == "posted" else Decimal("0")
    reference = f"stripe:{data.get('payment_intent') or data['id']}"
    allocations = [{"invoice": invoice, "amount": allocated, "reference": reference}] if allocated > 0 else []
    local.payment = record_customer_payment(
        invoice.customer, amount=amount, currency=currency, reference=reference,
        received_on=timezone.localdate(_from_timestamp(received_at)), allocations=allocations,
        cash_account="1010")
    local.status = StripeCheckoutSession.Status.PAID


def recover_checkout_sessions(*, client=None):
    """Settle active sessions from the Stripe API, for webhook events that never arrived.

    Stripe does not redeliver past events after a destination's event list changes, so
    a payment can complete without a webhook. Paid sessions are recorded exactly as the
    webhook would, dated when the payment was made; expired ones are marked expired.
    Returns the number of sessions moved to each status.
    """
    client = client or stripe_client()
    moved = Counter()
    for pk, session_id in StripeCheckoutSession.objects.filter(
            status__in=StripeCheckoutSession.ACTIVE).values_list("pk", "stripe_session_id"):
        data = client.v1.checkout.sessions.retrieve(session_id, {"expand": ["payment_intent"]}).to_dict()
        intent = data.get("payment_intent") or {}
        data["payment_intent"] = intent.get("id")
        with transaction.atomic():
            local = StripeCheckoutSession.objects.select_for_update().get(pk=pk)
            if local.status not in StripeCheckoutSession.ACTIVE:
                continue  # The webhook settled it meanwhile.
            if data["status"] == "complete" and data["payment_status"] in PAID_STATUSES:
                _record_payment(local, data, received_at=intent.get("created") or data["created"])
            elif data["status"] == "expired":
                local.status = StripeCheckoutSession.Status.EXPIRED
            else:
                continue
            local.payment_intent_id = data["payment_intent"] or local.payment_intent_id
            local.save()
            moved[str(local.status)] += 1
    return dict(moved)
