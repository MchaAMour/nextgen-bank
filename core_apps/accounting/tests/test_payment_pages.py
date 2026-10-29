from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from core_apps.accounting.models import StripeCheckoutSession
from .test_ar_workflow import invoice
from .test_stripe_payments import checkout, fake_stripe, posted, session_event, signed_post, stripe_settings

pytestmark = pytest.mark.django_db
Status = StripeCheckoutSession.Status


def page(path, **params):
    return Client().get(path, params)


def set_status(session, status, **fields):
    if status == Status.PAID:  # Only the webhook can mark a session paid: it records the payment.
        assert signed_post(session_event(session)).status_code == 200
    else:
        StripeCheckoutSession.objects.filter(pk=session.pk).update(status=status, **fields)


def test_cancel_url_names_the_invoice(checkout, fake_stripe):
    params = fake_stripe.v1.checkout.sessions.create.call_args.args[0]
    assert params["cancel_url"] == f"http://localhost:8080/payments/cancelled?invoice={checkout.invoice_id}"
    assert params["success_url"].endswith("?session_id={CHECKOUT_SESSION_ID}")


@pytest.mark.parametrize("status, heading, refreshes", [
    (Status.OPEN, "Confirming your payment", True),  # Back before the webhook.
    (Status.PAID, "Payment received", False),
    (Status.AWAITING_PAYMENT, "Payment processing", False),
    (Status.FAILED, "Payment failed", False),
])
def test_success_page_shows_the_recorded_status(checkout, status, heading, refreshes):
    set_status(checkout, status)
    response = page("/payments/success", session_id=checkout.stripe_session_id)
    body = response.content.decode()
    assert response.status_code == 200 and heading in body and "AR-1" in body and "100.00 EUR" in body
    assert ('http-equiv="refresh"' in body) is refreshes
    assert "no-cache" in response["Cache-Control"]


@pytest.mark.parametrize("session_id", ["", "cs_test_unknown", "{CHECKOUT_SESSION_ID}"])
def test_success_page_for_an_unknown_session_is_not_found(session_id):
    response = page("/payments/success", session_id=session_id)
    assert response.status_code == 404 and "Payment not found" in response.content.decode()


def test_cancel_page_offers_the_open_session_until_it_expires(checkout):
    body = page("/payments/cancelled", invoice=checkout.invoice_id).content.decode()
    assert "Payment cancelled" in body and f'href="{checkout.url}"' in body
    set_status(checkout, Status.OPEN, expires_at=timezone.now() - timedelta(minutes=1))
    body = page("/payments/cancelled", invoice=checkout.invoice_id).content.decode()
    assert "Payment cancelled" in body and "Return to payment" not in body


def test_cancel_page_after_payment_shows_it_was_paid(checkout):
    set_status(checkout, Status.PAID)
    assert "Payment received" in page("/payments/cancelled", invoice=checkout.invoice_id).content.decode()


@pytest.mark.parametrize("invoice_param", ["", "not-a-uuid", "6f1c7f56-0000-4000-8000-000000000000"])
def test_cancel_page_without_a_known_invoice_shows_no_details(invoice_param):
    response = page("/payments/cancelled", invoice=invoice_param)
    assert response.status_code == 200 and "Payment cancelled" in response.content.decode()
    assert "Invoice" not in response.content.decode()
