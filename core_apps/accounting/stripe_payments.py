"""Stripe Checkout for AR invoices.

Sessions are created for an invoice's open balance. Payments are recorded only
from verified webhook events: Stripe clearing (1010) is debited and AR credited,
then the payment is allocated to the invoice up to its open balance at that time.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

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


def stripe_client():
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured("STRIPE_SECRET_KEY is not configured.")
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY)


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
        "cancel_url": settings.STRIPE_CHECKOUT_CANCEL_URL,
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
            _record_payment(local, data, event)
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


def _record_payment(local, data, event):
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
        received_on=timezone.localdate(_from_timestamp(event["created"])), allocations=allocations,
        cash_account="1010")
    local.status = StripeCheckoutSession.Status.PAID
