from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from core_apps.accounting.models import Customer, Invoice, JournalEntry, JournalLine
from core_apps.accounting.services import (create_draft_invoice, post_invoice,
    cancel_invoice, receive_invoice_payment, invoice_aging)

pytestmark = pytest.mark.django_db
DAY = date(2026, 1, 1)


@pytest.fixture
def invoice():
    customer = Customer.objects.create(name="AR customer")
    return create_draft_invoice(customer=customer, number="AR-1", issue_date=DAY,
        due_date=date(2026, 1, 31), lines=[dict(description="Work", quantity="1", unit_price="100")])


def test_posting_and_partial_payment_replay(invoice):
    invoice = post_invoice(invoice)
    assert invoice.open_balance == Decimal("100")
    lines = invoice.journal_entry.lines.all()
    assert sum(l.debit for l in lines) == sum(l.credit for l in lines) == Decimal("100")
    args = dict(amount="40", reference="R1", received_on=DAY)
    receipt = receive_invoice_payment(invoice, **args)
    assert receive_invoice_payment(invoice, **args).pk == receipt.pk
    assert invoice.open_balance == Decimal("60")
    assert JournalEntry.objects.count() == 2
    with pytest.raises(ValidationError):
        receive_invoice_payment(invoice, **{**args, "amount": "30"})
    with pytest.raises(ValidationError):
        receive_invoice_payment(invoice, amount="61", reference="R2", received_on=DAY)
    receive_invoice_payment(invoice, amount="60", reference="R2", received_on=DAY)
    assert invoice.open_balance == 0


def test_posting_failure_rolls_back(invoice):
    with patch("core_apps.accounting.services.post_journal_entry", side_effect=ValidationError("failure")):
        with pytest.raises(ValidationError):
            post_invoice(invoice)
    invoice.refresh_from_db()
    assert invoice.status == "draft"
    assert not JournalEntry.objects.exists()
    assert not JournalLine.objects.exists()


def test_posted_document_immutable_and_duplicate_post_rejected(invoice):
    post_invoice(invoice)
    with pytest.raises(ValidationError):
        post_invoice(invoice)
    with pytest.raises(ValidationError):
        invoice.save()
    with pytest.raises(ValidationError):
        invoice.lines.first().save()


def test_cancellation_offsets_ledger_and_historical_aging(invoice):
    invoice = post_invoice(invoice)
    cancel_invoice(invoice, cancellation_date=date(2026, 3, 1))
    lines = JournalLine.objects.filter(account__code="1100")
    assert sum(l.debit - l.credit for l in lines) == 0
    assert invoice_aging(as_of=date(2026, 2, 15))["EUR"]["1_30"] == Decimal("100")
    assert invoice_aging(as_of=date(2026, 3, 1)) == {}
    with pytest.raises(ValidationError):
        cancel_invoice(invoice, cancellation_date=date(2026, 3, 2))


def test_paid_invoice_cannot_be_cancelled(invoice):
    invoice = post_invoice(invoice)
    receive_invoice_payment(invoice, amount="10", reference="R", received_on=DAY)
    with pytest.raises(ValidationError):
        cancel_invoice(invoice, cancellation_date=DAY)
    assert JournalEntry.objects.count() == 2


@pytest.mark.parametrize("as_of,bucket", [(date(2026,1,31),"current"), (date(2026,2,1),"1_30"),
    (date(2026,3,3),"31_60"), (date(2026,4,2),"61_90"), (date(2026,5,2),"over_90")])
def test_aging_buckets_exclude_future_receipts(invoice, as_of, bucket):
    invoice = post_invoice(invoice)
    receive_invoice_payment(invoice, amount="25", reference="R", received_on=date(2026,6,1))
    assert invoice_aging(as_of=as_of)["EUR"][bucket] == Decimal("100")
    assert invoice_aging(as_of=date(2026,6,1))["EUR"]["total"] == Decimal("75")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "1.001", "1e50", 1.1])
def test_invalid_receipts_rejected(invoice, amount):
    invoice = post_invoice(invoice)
    with pytest.raises(ValidationError):
        receive_invoice_payment(invoice, amount=amount, reference="R", received_on=DAY)
    assert JournalEntry.objects.count() == 1
