import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction

from core_apps.accounting.models import ARQuerySet, CustomerPayment, Invoice, InvoiceLine, PaymentAllocation
from core_apps.accounting.services import (_post_pair, cancel_invoice, create_draft_invoice, post_invoice,
    receive_invoice_payment)
from .test_ar_workflow import DAY, invoice

pytestmark = pytest.mark.django_db


def db_id(obj):
    return obj._meta.pk.get_db_prep_value(obj.pk, connection)


def new_id():
    return Invoice._meta.pk.get_db_prep_value(uuid.uuid4(), connection)


def raw(sql, *params):
    """Execute SQL that bypasses every model guard; only the database can reject it."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.fixture
def posted(invoice):
    return post_invoice(invoice)


@pytest.fixture
def payment(posted):
    return receive_invoice_payment(posted, amount="40", reference="R-INT", received_on=DAY)


@pytest.mark.parametrize("model", [Invoice, InvoiceLine, CustomerPayment, PaymentAllocation])
@pytest.mark.parametrize("operation", ["update", "delete", "bulk_update", "bulk_create"])
def test_bulk_ar_writes_are_rejected(payment, model, operation):
    allocation = payment.allocations.get()
    obj = {Invoice: allocation.invoice, InvoiceLine: allocation.invoice.lines.first(),
           CustomerPayment: payment, PaymentAllocation: allocation}[model]
    with pytest.raises(ValidationError, match="Bulk AR document"):
        if operation == "update":
            model.objects.filter(pk=obj.pk).update(updated_at=obj.updated_at)
        elif operation == "delete":
            model.objects.filter(pk=obj.pk).delete()
        elif operation == "bulk_update":
            model.objects.bulk_update([obj], ["updated_at"])
        else:
            model.objects.bulk_create([obj])
    assert model.objects.filter(pk=obj.pk).exists()


@pytest.mark.parametrize("sql", [
    "UPDATE accounting_invoice SET posted_total = 1 WHERE id = %s",
    "UPDATE accounting_invoice SET due_date = '2027-01-01' WHERE id = %s",
    "UPDATE accounting_invoice SET status = 'draft', journal_entry_id = NULL, posted_total = NULL WHERE id = %s",
    "UPDATE accounting_invoice SET status = 'cancelled' WHERE id = %s",
    "DELETE FROM accounting_invoice WHERE id = %s",
    "UPDATE accounting_invoiceline SET unit_price = 1 WHERE invoice_id = %s",
    "DELETE FROM accounting_invoiceline WHERE invoice_id = %s",
])
def test_raw_sql_cannot_change_posted_invoice(posted, sql):
    with pytest.raises(IntegrityError):
        raw(sql, db_id(posted))
    posted.refresh_from_db()
    assert (posted.status, posted.posted_total, posted.lines.get().unit_price) == (
        "posted", Decimal("100.00"), Decimal("100.00"))


def test_raw_sql_cannot_add_line_to_posted_invoice(posted):
    line = posted.lines.get()
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_invoiceline (id, created_at, updated_at, invoice_id, description,"
            " quantity, unit_price) SELECT %s, created_at, updated_at, invoice_id, 'Extra', 1, 5"
            " FROM accounting_invoiceline WHERE id = %s",
            new_id(), db_id(line))
    assert posted.lines.count() == 1


@pytest.mark.parametrize("sql", [
    "UPDATE accounting_customerpayment SET amount = 1 WHERE id = %s",
    "DELETE FROM accounting_customerpayment WHERE id = %s",
    "UPDATE accounting_paymentallocation SET amount = 1 WHERE payment_id = %s",
    "DELETE FROM accounting_paymentallocation WHERE payment_id = %s",
])
def test_raw_sql_cannot_change_payments(payment, sql):
    with pytest.raises(IntegrityError):
        raw(sql, db_id(payment))
    payment = CustomerPayment.objects.with_balances().get(pk=payment.pk)
    assert (payment.amount, payment.allocated_total) == (Decimal("40.00"), Decimal("40.00"))


@pytest.mark.parametrize("case", ["wrong_accounts", "wrong_amount"])
def test_raw_payment_insert_requires_matching_ledger_entry(payment, case):
    # An unused entry is required so the unique journal_entry column cannot be what rejects it.
    if case == "wrong_accounts":
        entry, amount = payment.allocations.get().invoice.journal_entry, Decimal("100.00")
    else:
        entry, amount = _post_pair(debit_code="1000", credit_code="1100", amount=Decimal("40.00"),
            currency="USD", source_type="customer_payment", source_id=None, entry_date=DAY,
            memo="Unused"), Decimal("60.01")
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_customerpayment (id, created_at, updated_at, customer_id, reference,"
            " amount, currency, received_on, journal_entry_id) VALUES (%s, %s, %s, %s, %s, %s, 'USD', %s, %s)",
            new_id(), payment.created_at, payment.created_at, db_id(payment.customer), f"RAW-{case}",
            amount, DAY, db_id(entry))
    assert CustomerPayment.objects.count() == 1


def test_raw_sql_cannot_post_without_posted_entry(invoice):
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_invoice SET status = 'posted', posted_total = 100,"
            " journal_entry_id = NULL WHERE id = %s", db_id(invoice))
    invoice.refresh_from_db()
    assert invoice.status == "draft"


def test_constraint_rejects_inconsistent_lifecycle_fields(invoice):
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_invoice SET posted_total = 5 WHERE id = %s", db_id(invoice))


def test_private_orm_bypasses_are_rejected(posted):
    posted.due_date = date(2027, 1, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        models.Model.save(posted)
    with pytest.raises(IntegrityError), transaction.atomic():
        models.QuerySet.update(Invoice.objects.filter(pk=posted.pk), status="cancelled")
    line = posted.lines.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        models.QuerySet.update(InvoiceLine.objects.filter(pk=line.pk), unit_price=Decimal("1"))
    posted.refresh_from_db()
    assert (posted.status, posted.due_date) == ("posted", date(2026, 1, 31))


def test_line_cannot_move_into_posted_invoice(posted):
    draft = create_draft_invoice(customer=posted.customer, number="AR-2", issue_date=DAY,
        due_date=DAY, lines=[dict(description="Draft", quantity="1", unit_price="5")])
    line = draft.lines.get()
    line.invoice = posted
    with pytest.raises(ValidationError):
        line.save()
    assert posted.lines.count() == 1


def test_draft_line_writes_lock_parent_invoice(invoice):
    locked = []
    original = ARQuerySet.select_for_update

    def spy(queryset, *args, **kwargs):
        locked.append(queryset.model)
        return original(queryset, *args, **kwargs)

    with patch.object(ARQuerySet, "select_for_update", autospec=True, side_effect=spy):
        line = invoice.lines.get()
        line.unit_price = Decimal("120")
        line.save()
        InvoiceLine.objects.create(invoice=invoice, description="Extra", quantity="1", unit_price="5")
        line.delete()
    assert locked == [Invoice, Invoice, Invoice]
    assert post_invoice(invoice).posted_total == Decimal("5.00")


def test_service_lifecycles_still_pass_database_checks(posted):
    draft = create_draft_invoice(customer=posted.customer, number="AR-3", issue_date=DAY,
        due_date=DAY, lines=[dict(description="Draft", quantity="1", unit_price="5")])
    assert cancel_invoice(draft, cancellation_date=DAY).status == "cancelled"
    cancelled = cancel_invoice(posted, cancellation_date=DAY)
    assert cancelled.cancellation_entry.reversal_of_id == cancelled.journal_entry_id
    other = create_draft_invoice(customer=posted.customer, number="AR-4", issue_date=DAY,
        due_date=DAY, lines=[dict(description="Draft", quantity="1", unit_price="5")])
    other.delete()
    assert not Invoice.objects.filter(number="AR-4").exists()
