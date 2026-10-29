from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum

from core_apps.accounting.models import (CreditNote, Customer, CustomerPayment, Invoice, JournalEntry,
    JournalLine, PaymentAllocation, PaymentRefund)
from core_apps.accounting.services import (_post_pair, allocate_payment, cancel_invoice, create_draft_invoice,
    invoice_aging, issue_credit_note, post_invoice, record_customer_payment, refund_payment)
from .test_api import client, user
from .test_ar_integrity import db_id, new_id, raw
from .test_ar_write_api import WRITE_PERMISSIONS, grant
from .test_ledger_integrity import starter_chart

pytestmark = pytest.mark.django_db
DAY = date(2026, 1, 1)
BASE = "/api/v1/accounting/"


@pytest.fixture
def customer():
    return Customer.objects.create(name="Settlement customer")


@pytest.fixture
def make_invoice(customer):
    def make(number, amount="100", currency="EUR", owner=None, post=True):
        invoice = create_draft_invoice(customer=owner or customer, number=number, issue_date=DAY,
            due_date=date(2026, 1, 31), currency=currency,
            lines=[dict(description="Work", quantity="1", unit_price=amount)])
        return post_invoice(invoice) if post else invoice
    return make


def pay(customer, amount, reference="P1", on=DAY, currency="EUR", allocations=()):
    return record_customer_payment(customer, amount=amount, currency=currency, reference=reference,
                                   received_on=on, allocations=allocations)


def balance(model, pk):
    return model.objects.with_balances().get(pk=pk)


def ar_ledger_balance(as_of):
    totals = JournalLine.objects.filter(account__code="1100", entry__entry_date__lte=as_of).aggregate(
        debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or 0) - (totals["credit"] or 0)


def test_one_payment_settles_several_invoices_and_keeps_credit(customer, make_invoice):
    first, second, third = make_invoice("S-1", "100"), make_invoice("S-2", "80"), make_invoice("S-3", "50")
    payment = pay(customer, "200", allocations=[
        {"invoice": first, "amount": "100", "reference": "A1"},
        {"invoice": second, "amount": "30", "reference": "A2"}])
    assert JournalEntry.objects.filter(source_type="customer_payment").count() == 1
    assert (balance(Invoice, first.pk).open_balance, balance(Invoice, second.pk).open_balance) == (0, 50)
    assert balance(CustomerPayment, payment.pk).unapplied_amount == Decimal("70")
    allocate_payment(payment, invoice=third, amount="50", reference="A3", allocated_on=date(2026, 1, 5))
    allocate_payment(payment, invoice=second, amount="20", reference="A4", allocated_on=date(2026, 1, 5))
    assert balance(CustomerPayment, payment.pk).unapplied_amount == 0
    assert balance(Invoice, second.pk).open_balance == Decimal("30")
    assert JournalEntry.objects.filter(source_type="customer_payment").count() == 1


def test_one_invoice_paid_by_several_payments(customer, make_invoice):
    invoice = make_invoice("S-1", "100")
    pay(customer, "60", "P1", allocations=[{"invoice": invoice, "amount": "60", "reference": "A1"}])
    pay(customer, "40", "P2", allocations=[{"invoice": invoice, "amount": "40", "reference": "A2"}])
    assert balance(Invoice, invoice.pk).open_balance == 0
    with pytest.raises(ValidationError):
        pay(customer, "1", "P3", allocations=[{"invoice": invoice, "amount": "1", "reference": "A3"}])
    assert not CustomerPayment.objects.filter(reference="P3").exists()


@pytest.mark.parametrize("problem", ["other_customer", "draft", "before_payment",
                                     "over_invoice", "over_payment", "float"])
def test_invalid_allocations_are_rejected(customer, make_invoice, problem):
    payment = pay(customer, "50", on=date(2026, 1, 10))
    other = Customer.objects.create(name="Other")
    invoice = {"other_customer": lambda: make_invoice("X", owner=other),
               "draft": lambda: make_invoice("X", post=False)}.get(problem, lambda: make_invoice("X", "30"))()
    amount = {"over_invoice": "30.01", "over_payment": "30", "float": 1.5}.get(problem, "10")
    if problem == "over_payment":
        refund_payment(payment, amount="25", reference="RF", refunded_on=date(2026, 1, 10))
    on = date(2026, 1, 9) if problem == "before_payment" else date(2026, 1, 10)
    with pytest.raises(ValidationError):
        allocate_payment(payment, invoice=invoice, amount=amount, reference="A", allocated_on=on)
    assert not PaymentAllocation.objects.exists()


def test_payment_replays_and_conflicts(customer, make_invoice):
    invoice = make_invoice("S-1")
    allocations = [{"invoice": invoice, "amount": "40", "reference": "A1"}]
    payment = pay(customer, "50", allocations=allocations)
    assert pay(customer, "50", allocations=allocations).pk == payment.pk
    assert pay(customer, "50").pk == payment.pk  # Allocations already applied are not required again.
    for changes in [dict(amount="51"), dict(on=date(2026, 1, 2)), dict(currency="USD"),
                    dict(allocations=[{**allocations[0], "amount": "41"}]),
                    dict(allocations=allocations + [{"invoice": invoice, "amount": "1", "reference": "A2"}])]:
        with pytest.raises(ValidationError):
            pay(customer, **{"amount": "50", **changes})
    assert (CustomerPayment.objects.count(), PaymentAllocation.objects.count(), JournalEntry.objects.count()) == (
        1, 1, 2)
    later = dict(invoice=invoice, amount="10", reference="A9", allocated_on=date(2026, 1, 3))
    assert allocate_payment(payment, **later).pk == allocate_payment(payment, **later).pk
    with pytest.raises(ValidationError):
        allocate_payment(payment, **{**later, "amount": "5"})


def test_failed_allocation_rolls_back_payment(customer, make_invoice):
    invoice = make_invoice("S-1")
    with pytest.raises(ValidationError):
        pay(customer, "150", allocations=[{"invoice": invoice, "amount": "150", "reference": "A1"}])
    assert not CustomerPayment.objects.exists() and not JournalEntry.objects.exclude(pk=invoice.journal_entry_id).exists()


def test_refunds_return_only_unapplied_credit(customer, make_invoice):
    invoice = make_invoice("S-1", "60")
    payment = pay(customer, "100", allocations=[{"invoice": invoice, "amount": "60", "reference": "A1"}])
    args = dict(amount="30", reference="RF1", refunded_on=date(2026, 1, 2))
    refund = refund_payment(payment, **args)
    assert refund_payment(payment, **args).pk == refund.pk
    lines = {line.account.code: (line.debit, line.credit) for line in refund.journal_entry.lines.all()}
    assert lines == {"1100": (Decimal("30"), 0), "1000": (0, Decimal("30"))}
    for bad in [dict(amount="10.01", reference="RF2"), dict(amount="1", reference="RF3", refunded_on=date(2025, 12, 31)),
                dict(amount="1", reference="RF1")]:
        with pytest.raises(ValidationError):
            refund_payment(payment, **{**args, **bad})
    refund_payment(payment, amount="10", reference="RF4", refunded_on=date(2026, 1, 2))
    assert balance(CustomerPayment, payment.pk).unapplied_amount == 0


def test_credit_notes_reduce_open_balance(customer, make_invoice):
    invoice = make_invoice("S-1", "100")
    pay(customer, "30", allocations=[{"invoice": invoice, "amount": "30", "reference": "A1"}])
    args = dict(number="CN-1", amount="20", issued_on=date(2026, 1, 3), reason="Discount")
    note = issue_credit_note(invoice, **args)
    assert issue_credit_note(invoice, **args).pk == note.pk
    lines = {line.account.code: (line.debit, line.credit) for line in note.journal_entry.lines.all()}
    assert lines == {"4000": (Decimal("20"), 0), "1100": (0, Decimal("20"))}
    assert balance(Invoice, invoice.pk).open_balance == Decimal("50")
    for bad in [dict(number="CN-2", amount="50.01"), dict(number="CN-3", issued_on=date(2025, 12, 31)),
                dict(reason="Other")]:
        with pytest.raises(ValidationError):
            issue_credit_note(invoice, **{**args, **bad})
    with pytest.raises(ValidationError):
        issue_credit_note(make_invoice("S-2", post=False), **{**args, "number": "CN-4"})
    issue_credit_note(invoice, **{**args, "number": "CN-5", "amount": "50"})
    assert balance(Invoice, invoice.pk).open_balance == 0
    assert CreditNote.objects.count() == 2


@pytest.mark.parametrize("settlement", ["allocation", "credit_note"])
def test_settled_invoices_cannot_be_cancelled(customer, make_invoice, settlement):
    invoice = make_invoice("S-1")
    if settlement == "allocation":
        pay(customer, "10", allocations=[{"invoice": invoice, "amount": "10", "reference": "A1"}])
    else:
        issue_credit_note(invoice, number="CN-1", amount="10", issued_on=DAY, reason="Fix")
    with pytest.raises(ValidationError):
        cancel_invoice(invoice, cancellation_date=DAY)
    # A correct reversing entry still cannot cancel it through raw SQL.
    reversal = _post_pair(debit_code="4000", credit_code="1100", amount=Decimal("100"), currency="EUR",
        source_type="invoice", source_id=invoice.pk, entry_date=DAY, memo="Raw", reversal_of=invoice.journal_entry)
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_invoice SET status = 'cancelled', cancellation_entry_id = %s WHERE id = %s",
            db_id(reversal), db_id(invoice))
    assert Invoice.objects.get(pk=invoice.pk).status == "posted"


def test_aging_reports_unapplied_credit_and_reconciles_to_ledger(customer, make_invoice):
    first, second = make_invoice("S-1", "100"), make_invoice("S-2", "80")
    payment = pay(customer, "150", on=date(2026, 1, 10),
                  allocations=[{"invoice": first, "amount": "100", "reference": "A1"}])
    allocate_payment(payment, invoice=second, amount="30", reference="A2", allocated_on=date(2026, 2, 5))
    refund_payment(payment, amount="20", reference="RF1", refunded_on=date(2026, 2, 10))
    issue_credit_note(second, number="CN-1", amount="15", issued_on=date(2026, 3, 1), reason="Discount")
    cancelled = make_invoice("S-3", "40")
    cancel_invoice(cancelled, cancellation_date=date(2026, 1, 20))
    # S-3 is still open on 15 January; its cancellation is dated 20 January.
    expected = {date(2026, 1, 15): (Decimal("120"), Decimal("50")),
                date(2026, 2, 5): (Decimal("50"), Decimal("20")),
                date(2026, 3, 1): (Decimal("35"), Decimal("0"))}
    for as_of, (total, unapplied) in expected.items():
        eur = invoice_aging(as_of=as_of)["EUR"]
        assert (eur["total"], eur["unapplied"]) == (total, unapplied)
        assert eur["net"] == total - unapplied == ar_ledger_balance(as_of)
    assert invoice_aging(as_of=date(2026, 1, 15))["EUR"]["current"] == Decimal("120")


@pytest.mark.parametrize("table", ["customerpayment", "paymentallocation", "paymentrefund", "creditnote"])
def test_raw_sql_cannot_change_settlements(customer, make_invoice, table):
    invoice = make_invoice("S-1")
    payment = pay(customer, "50", allocations=[{"invoice": invoice, "amount": "20", "reference": "A1"}])
    refund_payment(payment, amount="5", reference="RF1", refunded_on=DAY)
    issue_credit_note(invoice, number="CN-1", amount="5", issued_on=DAY, reason="Fix")
    for sql in (f"UPDATE accounting_{table} SET amount = 1", f"DELETE FROM accounting_{table}"):
        with pytest.raises(IntegrityError):
            raw(sql)
    assert [m.objects.get().amount for m in (PaymentAllocation, PaymentRefund, CreditNote)] == [
        Decimal("20"), Decimal("5"), Decimal("5")]


@pytest.mark.parametrize("amount", ["30.01", "45.01"])
def test_raw_allocation_cannot_exceed_balances(customer, make_invoice, amount):
    # 30.01 exceeds the invoice's open balance; 45.01 exceeds the payment's unapplied amount.
    invoice = make_invoice("S-1", "30" if amount == "30.01" else "100")
    payment = pay(customer, "50")
    refund_payment(payment, amount="5", reference="RF1", refunded_on=DAY)
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_paymentallocation (id, created_at, updated_at, payment_id, invoice_id,"
            " reference, amount, allocated_on) VALUES (%s, %s, %s, %s, %s, 'RAW', %s, %s)",
            new_id(), payment.created_at, payment.created_at, db_id(payment), db_id(invoice), Decimal(amount), DAY)
    assert not PaymentAllocation.objects.exists()


def test_raw_refund_and_credit_note_need_matching_entries(customer, make_invoice):
    invoice = make_invoice("S-1")
    payment = pay(customer, "50")
    # Reuse an entry with the right shape for the other record type: legs are in the wrong direction.
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_paymentrefund (id, created_at, updated_at, payment_id, reference, amount,"
            " refunded_on, journal_entry_id) VALUES (%s, %s, %s, %s, 'RAW', 50, %s, %s)",
            new_id(), payment.created_at, payment.created_at, db_id(payment), DAY, db_id(payment.journal_entry))
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_creditnote (id, created_at, updated_at, number, invoice_id, amount, issued_on,"
            " reason, journal_entry_id) VALUES (%s, %s, %s, 'RAW', %s, 50, %s, 'x', %s)",
            new_id(), payment.created_at, payment.created_at, db_id(invoice), DAY, db_id(payment.journal_entry))
    assert not PaymentRefund.objects.exists() and not CreditNote.objects.exists()


def delete_ar_rows_with_triggers_suspended():
    """SQLite flushes with DELETE, which the AR triggers reject; PostgreSQL TRUNCATE skips them."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'accounting_%'")
        triggers = cursor.fetchall()
        for name, _ in triggers:
            cursor.execute(f"DROP TRIGGER {name}")
        for table in ["auditevent", "paymentallocation", "paymentrefund", "creditnote", "customerpayment", "invoiceline",
                      "invoice", "journalline", "journalentry", "customer"]:
            cursor.execute(f"DELETE FROM accounting_{table}")
        for _, sql in triggers:
            cursor.execute(sql)


@pytest.mark.django_db(transaction=True)
def test_receipts_migrate_to_single_allocation_payments(starter_chart):
    executor = MigrationExecutor(connection)
    before, after = [("accounting", "0005_ar_document_integrity")], [("accounting", "0006_ar_settlements")]
    executor.migrate(before)
    try:
        apps = executor.loader.project_state(before).apps
        customer = apps.get_model("accounting", "Customer").objects.create(name="Legacy")
        invoice = apps.get_model("accounting", "Invoice").objects.create(
            customer=customer, number="LEGACY-1", issue_date=DAY, due_date=DAY, currency="EUR")
        apps.get_model("accounting", "InvoiceLine").objects.create(
            invoice=invoice, description="Work", quantity=1, unit_price=Decimal("100"))
        invoice = Invoice.objects.get(pk=invoice.pk)
        # The 0005 schema has no audit or period tables.
        with patch("core_apps.accounting.services.audit"), \
                patch("core_apps.accounting.services.books_closed_through", return_value=None):
            post_invoice(invoice)
            entry = _post_pair(debit_code="1000", credit_code="1100", amount=Decimal("40"), currency="EUR",
                source_type="invoice", source_id=invoice.pk, entry_date=DAY, memo="Receipt R-OLD")
        apps.get_model("accounting", "InvoiceReceipt").objects.create(invoice_id=invoice.pk, reference="R-OLD",
            amount=Decimal("40"), received_on=DAY, journal_entry_id=entry.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        # Current models query tables added since 0006.
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        payment = CustomerPayment.objects.with_balances().get(reference="R-OLD")
        allocation = payment.allocations.get()
        assert (payment.journal_entry_id, payment.amount, payment.unapplied_amount) == (entry.pk, 40, 0)
        assert (allocation.invoice_id, allocation.reference, allocation.amount) == (invoice.pk, "R-OLD", 40)
        assert Invoice.objects.with_balances().get(pk=invoice.pk).open_balance == Decimal("60")
    finally:
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        delete_ar_rows_with_triggers_suspended()


def test_settlement_api(client, user, customer, make_invoice):
    grant(user, *WRITE_PERMISSIONS)
    first, second = make_invoice("S-1", "100"), make_invoice("S-2", "80")
    body = {"customer": str(customer.pk), "reference": "API-P", "amount": "150.00", "currency": "EUR",
            "received_on": "2026-01-10", "allocations": [
                {"invoice": str(first.pk), "amount": "100.00", "reference": "API-A1"}]}
    created = client.post(BASE + "payments/", body, format="json")
    assert (created.status_code, created.data["unapplied_amount"]) == (201, "50.00")
    assert client.post(BASE + "payments/", body, format="json").status_code == 200
    url = f"{BASE}payments/{created.data['id']}/"
    allocation = {"invoice": str(second.pk), "amount": "30.00", "reference": "API-A2", "allocated_on": "2026-01-11"}
    assert client.post(url + "allocations/", allocation, format="json").status_code == 201
    assert client.post(url + "allocations/", allocation, format="json").status_code == 200
    over = {**allocation, "reference": "API-A3", "amount": "20.01"}
    assert client.post(url + "allocations/", over, format="json").status_code == 400
    refund = {"reference": "API-RF", "amount": "20.00", "refunded_on": "2026-01-12"}
    assert client.post(url + "refunds/", refund, format="json").status_code == 201
    assert client.post(url + "refunds/", refund, format="json").status_code == 200
    assert client.post(url + "refunds/", {**refund, "reference": "X", "amount": "0.01"},
                       format="json").status_code == 400
    note = {"number": "API-CN", "invoice": str(second.pk), "amount": "50.00", "issued_on": "2026-01-12",
            "reason": "Settlement discount"}
    assert client.post(BASE + "credit-notes/", note, format="json").status_code == 201
    assert client.post(BASE + "credit-notes/", note, format="json").status_code == 200
    assert client.post(BASE + "credit-notes/", {**note, "amount": "1.00"}, format="json").status_code == 400
    detail = client.get(url).data
    assert (detail["unapplied_amount"], len(detail["allocations"]), len(detail["refunds"])) == ("0.00", 2, 1)
    invoices = {row["number"]: row["open_balance"] for row in client.get(BASE + "invoices/").data["results"]}
    assert invoices == {"S-1": "0.00", "S-2": "0.00"}
    aging = client.get(BASE + "ar-aging/", {"as_of": "2026-01-12"}).data["currencies"]
    assert aging == {} or aging["EUR"]["net"] == "0.00"


@pytest.mark.parametrize("path,permission", [("allocations/", "add_paymentallocation"),
                                             ("refunds/", "add_paymentrefund")])
def test_settlement_writes_require_their_permission(client, user, customer, path, permission):
    payment = pay(customer, "10")
    grant(user, *(p for p in WRITE_PERMISSIONS if p != permission))
    assert client.post(f"{BASE}payments/{payment.pk}/{path}", {}, format="json").status_code == 403
    assert not PaymentAllocation.objects.exists() and not PaymentRefund.objects.exists()


def test_invoice_list_balances_use_constant_queries(client, customer, make_invoice, django_assert_max_num_queries):
    for number in range(5):
        invoice = make_invoice(f"Q-{number}")
        pay(customer, "10", f"QP-{number}", allocations=[
            {"invoice": invoice, "amount": "10", "reference": f"QA-{number}"}])
    with django_assert_max_num_queries(8):
        response = client.get(BASE + "invoices/")
    assert {row["open_balance"] for row in response.data["results"]} == {"90.00"}
