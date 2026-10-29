from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

from core_apps.accounting.models import AllocationRelease, AuditEvent, CustomerPayment, Invoice, JournalEntry, PeriodClose
from core_apps.accounting.services import (allocate_payment, cancel_invoice, invoice_aging, post_invoice,
    record_customer_payment, refund_payment, release_allocation)
from .test_ar_integrity import db_id, new_id, raw
from .test_ar_workflow import invoice
from .test_ar_write_api import grant
from .test_api import client, user

pytestmark = pytest.mark.django_db
JAN_10, FEB_1 = date(2026, 1, 10), date(2026, 2, 1)


@pytest.fixture
def paid(invoice):
    """Invoice AR-1 (100) fully paid by a 100 payment received on 10 January."""
    invoice = post_invoice(invoice)
    payment = record_customer_payment(invoice.customer, amount="100", currency="EUR", reference="P-1",
                                      received_on=JAN_10, allocations=[
                                          {"invoice": invoice, "amount": "100", "reference": "A-1"}])
    return payment.allocations.get()


def balances(allocation):
    invoice = Invoice.objects.with_balances().get(pk=allocation.invoice_id)
    payment = CustomerPayment.objects.with_balances().get(pk=allocation.payment_id)
    # Annotated and unannotated balances agree.
    assert (Invoice.objects.get(pk=invoice.pk).open_balance,
            CustomerPayment.objects.get(pk=payment.pk).unapplied_amount) == (invoice.open_balance,
                                                                              payment.unapplied_amount)
    return invoice.open_balance, payment.unapplied_amount


def test_release_reopens_the_invoice_without_a_journal_entry(paid):
    entries = JournalEntry.objects.count()
    assert balances(paid) == (0, 0)
    release = release_allocation(paid, released_on=FEB_1, reason="Applied to the wrong invoice")
    assert balances(paid) == (Decimal("100"), Decimal("100"))
    assert JournalEntry.objects.count() == entries
    event = AuditEvent.objects.get(action="payment.allocation_released")
    assert event.details["amount"] == "100.00" and event.details["reason"] == "Applied to the wrong invoice"

    assert release_allocation(paid, released_on=FEB_1, reason="Applied to the wrong invoice") == release
    assert AuditEvent.objects.filter(action="payment.allocation_released").count() == 1  # Replays record nothing.
    with pytest.raises(ValidationError, match="already been released"):
        release_allocation(paid, released_on=FEB_1, reason="Different reason")


def test_released_credit_can_be_reapplied_refunded_and_the_invoice_cancelled(paid):
    release_allocation(paid, released_on=FEB_1, reason="Customer asked for a refund")
    payment, invoice = paid.payment, paid.invoice
    allocate_payment(payment, invoice=invoice, amount="30", reference="A-2", allocated_on=FEB_1)
    assert balances(paid) == (Decimal("70"), Decimal("70"))
    refund_payment(payment, amount="70", reference="RF-1", refunded_on=FEB_1)
    with pytest.raises(ValidationError):  # A-2 is still applied.
        cancel_invoice(invoice, cancellation_date=FEB_1)
    release_allocation(payment.allocations.get(reference="A-2"), released_on=FEB_1, reason="Cancelling")
    cancel_invoice(Invoice.objects.get(pk=invoice.pk), cancellation_date=FEB_1)
    assert balances(paid) == (0, Decimal("30"))


@pytest.mark.parametrize("released_on, message", [
    (date(2026, 1, 9), "cannot precede its allocation"),
    (date(2026, 1, 31), "closed through 2026-01-31"),
])
def test_release_date_must_follow_the_allocation_and_be_in_an_open_period(paid, released_on, message):
    PeriodClose.objects.create(closed_through=date(2026, 1, 31))
    with pytest.raises(ValidationError, match=message):
        release_allocation(paid, released_on=released_on, reason="Late")
    with pytest.raises(ValidationError, match="reason"):
        release_allocation(paid, released_on=FEB_1, reason=" ")


def test_aging_counts_the_release_from_its_date(paid):
    release_allocation(paid, released_on=date(2026, 3, 1), reason="Wrong customer")
    assert invoice_aging(as_of=date(2026, 2, 28)) == {}  # Settled: nothing open, no credit.
    after = invoice_aging(as_of=date(2026, 3, 1))["EUR"]
    assert (after["total"], after["unapplied"], after["net"]) == (Decimal("100"), Decimal("100"), 0)


def test_database_rejects_releases_the_service_would_refuse(paid):
    insert = ("INSERT INTO accounting_allocationrelease (id, created_at, updated_at, allocation_id, released_on, reason)"
              " VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, 'raw')")
    with pytest.raises(IntegrityError):
        raw(insert, new_id(), db_id(paid), "2026-01-09")
    PeriodClose.objects.create(closed_through=date(2026, 1, 31))
    with pytest.raises(IntegrityError):
        raw(insert, new_id(), db_id(paid), "2026-01-31")
    release = release_allocation(paid, released_on=FEB_1, reason="Correction")
    for sql in ("UPDATE accounting_allocationrelease SET reason = 'x' WHERE id = %s",
                "DELETE FROM accounting_allocationrelease WHERE id = %s"):
        with pytest.raises(IntegrityError):
            raw(sql, db_id(release))


def test_database_balance_checks_ignore_released_allocations(paid):
    """Raw SQL, so only the database's release-aware sums decide."""
    insert = ("INSERT INTO accounting_paymentallocation (id, created_at, updated_at, payment_id, invoice_id,"
              " reference, amount, allocated_on) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, 100,"
              " '2026-02-01')")
    payment, invoice = paid.payment, paid.invoice
    with pytest.raises(IntegrityError):  # Invoice and payment are settled.
        raw(insert, new_id(), db_id(payment), db_id(invoice), "RAW-1")
    release_allocation(paid, released_on=FEB_1, reason="Correction")
    raw(insert, new_id(), db_id(payment), db_id(invoice), "RAW-2")
    assert balances(paid) == (0, 0)


@pytest.mark.django_db(transaction=True)
def test_migration_round_trip_restores_the_original_triggers():
    def definitions():
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN"
                               " ('accounting_paymentallocation_guard_insert', 'accounting_invoice_guard_update')")
            else:
                cursor.execute("SELECT proname, prosrc FROM pg_proc WHERE proname IN"
                               " ('accounting_paymentallocation_guard', 'accounting_invoice_guard')")
            return dict(cursor.fetchall())

    current = definitions()
    assert all("accounting_allocationrelease" in sql for sql in current.values())
    executor = MigrationExecutor(connection)
    executor.migrate([("accounting", "0014_period_close")])
    try:
        before = definitions()
        assert before.keys() == current.keys()
        assert not any("allocationrelease" in sql for sql in before.values())
    finally:
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
    assert definitions() == current


def test_release_api(client, user, paid):
    url = f"/api/v1/accounting/allocations/{paid.pk}/release/"
    body = {"released_on": "2026-02-01", "reason": "Wrong invoice"}
    assert client.post(url, body, format="json").status_code == 403
    grant(user, "add_allocationrelease")
    client.force_authenticate(type(user).objects.get(pk=user.pk))
    created = client.post(url, body, format="json")
    assert created.status_code == 201 and created.data["reason"] == "Wrong invoice"
    assert AllocationRelease.objects.get().released_by_id == user.pk
    assert client.post(url, body, format="json").status_code == 200
    assert client.post(url, {**body, "reason": "Other"}, format="json").status_code == 400
    payment = client.get(f"/api/v1/accounting/payments/{paid.payment_id}/").data
    assert payment["unapplied_amount"] == "100.00"
    assert payment["allocations"][0]["release"]["released_on"] == "2026-02-01"
