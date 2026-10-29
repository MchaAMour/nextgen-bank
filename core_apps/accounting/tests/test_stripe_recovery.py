import time
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command

from core_apps.accounting.models import CustomerPayment, Invoice, JournalEntry
from core_apps.accounting.stripe_payments import recover_checkout_sessions
from .test_ar_workflow import invoice
from .test_stripe_payments import (checkout, fake_stripe, ledger_balanced, posted, session_event, signed_post,
                                   stripe_settings)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("stripe_settings")]
PAID_ON = int(time.mktime(date(2026, 1, 22).timetuple()))


def stripe_returns(fake_stripe, checkout, status="complete", payment_status="paid"):
    """What the Checkout Sessions API returns for the session, with its PaymentIntent expanded."""
    session = MagicMock()
    session.to_dict.return_value = {
        "id": checkout.stripe_session_id, "status": status, "payment_status": payment_status,
        "amount_total": int(checkout.amount * 100), "currency": "eur", "created": PAID_ON - 60,
        "payment_intent": {"id": "pi_test_1", "created": PAID_ON} if payment_status == "paid" else None}
    fake_stripe.v1.checkout.sessions.retrieve.return_value = session


def test_paid_session_without_a_webhook_is_recorded_once(fake_stripe, checkout):
    stripe_returns(fake_stripe, checkout)
    assert recover_checkout_sessions() == {"paid": 1}
    fake_stripe.v1.checkout.sessions.retrieve.assert_called_once_with(
        checkout.stripe_session_id, {"expand": ["payment_intent"]})
    checkout.refresh_from_db()
    payment = checkout.payment
    assert (checkout.status, checkout.payment_intent_id) == ("paid", "pi_test_1")
    assert (payment.reference, payment.amount, payment.received_on) == ("stripe:pi_test_1", 100, date(2026, 1, 22))
    lines = {line.account.code: (line.debit, line.credit) for line in payment.journal_entry.lines.all()}
    assert lines == {"1010": (Decimal("100"), 0), "1100": (0, Decimal("100"))}
    assert Invoice.objects.with_balances().get(pk=checkout.invoice_id).open_balance == 0

    entries = JournalEntry.objects.count()
    assert recover_checkout_sessions() == {}  # Paid sessions are no longer asked about.
    assert signed_post(session_event(checkout)).status_code == 200  # A late webhook changes nothing.
    assert (JournalEntry.objects.count(), CustomerPayment.objects.count()) == (entries, 1)
    assert ledger_balanced()


@pytest.mark.parametrize("status, payment_status, expected, local_status", [
    ("expired", "unpaid", {"expired": 1}, "expired"),
    ("open", "unpaid", {}, "open"),
    ("complete", "unpaid", {}, "open"),  # Asynchronous payment still pending: wait for it.
])
def test_unpaid_sessions_post_nothing(fake_stripe, checkout, status, payment_status, expected, local_status):
    stripe_returns(fake_stripe, checkout, status, payment_status)
    entries = JournalEntry.objects.count()
    assert recover_checkout_sessions() == expected
    checkout.refresh_from_db()
    assert (checkout.status, JournalEntry.objects.count()) == (local_status, entries)


def test_recover_command(fake_stripe, checkout, capsys):
    stripe_returns(fake_stripe, checkout)
    call_command("recover_stripe_checkouts")
    assert "{'paid': 1}" in capsys.readouterr().out
