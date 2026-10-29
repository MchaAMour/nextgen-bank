from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from rest_framework.test import APIClient

from core_apps.accounting.ap_services import (allocate_supplier_payment, approve_bill, bill_aging, cancel_bill,
    create_draft_bill, delete_draft_bill, post_bill, record_supplier_payment, replace_draft_bill,
    supplier_statement)
from core_apps.accounting.bank_reconciliation import import_bank_statement
from core_apps.accounting.models import AuditEvent, Bill, BillLine, ChartOfAccount, JournalEntry, JournalLine, Supplier
from .test_ar_integrity import db_id, new_id, raw
from .test_ar_write_api import grant

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"
DAY = date(2026, 1, 5)


def make_user(number):
    return get_user_model().objects.create(
        email=f"ap{number}@example.com", username=f"ap{number}", id_no=20000 + number, first_name="AP",
        last_name=f"User{number}", security_question="favorite_color", security_answer="blue")


@pytest.fixture
def clerk():
    return make_user(1)


@pytest.fixture
def approver():
    return make_user(2)


@pytest.fixture
def supplier():
    return Supplier.objects.create(name="Office Supplies Ltd", email="billing@supplies.example")


def lines(*items):
    return [{"account": code, "description": f"Item {code}", "quantity": quantity, "unit_price": price}
            for code, quantity, price in items]


def draft(supplier, clerk, number="SUP-1", items=(("5000", "2", "100.00"), ("5100", "1", "100.00")),
          bill_date=DAY, due_date=date(2026, 2, 4)):
    return create_draft_bill(supplier=supplier, number=number, bill_date=bill_date, due_date=due_date,
                             lines=lines(*items), actor=clerk)


@pytest.fixture
def posted(supplier, clerk, approver):
    return post_bill(approve_bill(draft(supplier, clerk), actor=approver), actor=approver)


def ap_balance(as_of=None):
    lines = JournalLine.objects.filter(account__code="2000", entry__status__in=JournalEntry.IN_LEDGER)
    if as_of:
        lines = lines.filter(entry__entry_date__lte=as_of)
    return sum((line.credit - line.debit for line in lines), Decimal("0"))


def test_bill_is_approved_by_someone_else_then_posted(supplier, clerk, approver):
    bill = draft(supplier, clerk)
    assert (bill.status, bill.total, bill.created_by) == ("draft", Decimal("300.00"), clerk)
    with pytest.raises(ValidationError, match="Only approved bills"):
        post_bill(bill)
    with pytest.raises(ValidationError, match="someone other than its creator"):
        approve_bill(bill, actor=clerk)
    with pytest.raises(ValidationError, match="signed-in user"):
        approve_bill(bill, actor=None)
    bill = approve_bill(bill, actor=approver)
    assert (bill.status, bill.approved_by) == ("approved", approver)
    assert bill.approved_at is not None
    with pytest.raises(ValidationError, match="Only draft bills can be edited"):
        replace_draft_bill(bill, supplier=supplier, number="SUP-1", bill_date=DAY, due_date=DAY, lines=lines())
    with pytest.raises(ValidationError, match="immutable"):
        BillLine.objects.create(bill=bill, account=ChartOfAccount.objects.get(code="5000"), description="Late",
                                quantity=1, unit_price=1)
    bill = post_bill(bill, actor=approver)
    entry = bill.journal_entry
    assert (bill.status, bill.posted_total, entry.entry_date, entry.source_type) == (
        "posted", Decimal("300.00"), DAY, "bill")
    assert {(l.account.code, l.debit, l.credit) for l in entry.lines.all()} == {
        ("5000", Decimal("200.00"), 0), ("5100", Decimal("100.00"), 0), ("2000", 0, Decimal("300.00"))}
    assert bill.open_balance == Decimal("300.00") and ap_balance() == Decimal("300.00")


@pytest.mark.parametrize("code", ["1100", "2000", "1000", "4000", "9999"])
def test_bill_lines_need_an_expense_or_asset_account(supplier, clerk, code):
    with pytest.raises(ValidationError, match=f"Account {code}"):
        draft(supplier, clerk, items=((code, "1", "10.00"),))
    assert not Bill.objects.exists()


def test_drafts_can_be_replaced_and_deleted(supplier, clerk):
    bill = draft(supplier, clerk)
    bill = replace_draft_bill(bill, supplier=supplier, number="SUP-1B", bill_date=DAY, due_date=DAY,
                              lines=lines(("5000", "1", "50.00")), actor=clerk)
    assert (bill.number, bill.total, bill.lines.count()) == ("SUP-1B", Decimal("50.00"), 1)
    delete_draft_bill(bill, actor=clerk)
    assert not Bill.objects.exists()
    assert list(AuditEvent.objects.order_by("created_at").values_list("action", flat=True)) == [
        "bill.drafted", "bill.replaced", "bill.deleted"]


def test_partial_payments_prepayment_and_aging_match_the_ap_ledger(supplier, clerk, approver, posted):
    first = record_supplier_payment(supplier, amount="100.00", reference="SP-1", paid_on=date(2026, 1, 20),
                                    allocations=[{"bill": posted, "amount": "100.00", "reference": "SP-1-A"}])
    assert {(l.account.code, l.debit, l.credit) for l in first.journal_entry.lines.all()} == {
        ("2000", Decimal("100.00"), 0), ("1000", 0, Decimal("100.00"))}
    posted.refresh_from_db()
    assert Bill.objects.with_balances().get(pk=posted.pk).open_balance == Decimal("200.00")
    assert record_supplier_payment(supplier, amount="100.00", reference="SP-1", paid_on=date(2026, 1, 20)) == first
    with pytest.raises(ValidationError, match="different details"):
        record_supplier_payment(supplier, amount="90.00", reference="SP-1", paid_on=date(2026, 1, 20))

    second = record_supplier_payment(supplier, amount="250.00", reference="SP-2", paid_on=date(2026, 3, 1))
    allocate_supplier_payment(second, bill=posted, amount="200.00", reference="SP-2-A",
                              allocated_on=date(2026, 3, 1))
    with pytest.raises(ValidationError, match="exceeds the open bill balance"):
        allocate_supplier_payment(second, bill=posted, amount="10.00", reference="SP-2-B",
                                  allocated_on=date(2026, 3, 1))
    assert second.unapplied_amount == Decimal("50.00")

    january = bill_aging(as_of=date(2026, 1, 31))["EUR"]
    assert (january["current"], january["total"], january["net"]) == (Decimal("200.00"),) * 3
    february = bill_aging(as_of=date(2026, 2, 28))["EUR"]
    assert february["1_30"] == Decimal("200.00")  # Due on 4 February.
    march = bill_aging(as_of=date(2026, 3, 31))["EUR"]
    assert (march["total"], march["unapplied"], march["net"]) == (0, Decimal("50.00"), Decimal("-50.00"))
    for as_of in (date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)):
        assert bill_aging(as_of=as_of)["EUR"]["net"] == ap_balance(as_of)


def test_supplier_statement_adds_up_to_the_ap_ledger(supplier, clerk, approver, posted):
    record_supplier_payment(supplier, amount="120.00", reference="SP-1", paid_on=date(2026, 1, 20),
                            allocations=[{"bill": posted, "amount": "120.00", "reference": "SP-1-A"}])
    other = post_bill(approve_bill(draft(supplier, clerk, number="SUP-2", items=(("5000", "1", "40.00"),),
                                         bill_date=date(2026, 1, 25)), actor=approver))
    cancel_bill(other, cancellation_date=date(2026, 1, 26))
    statement = supplier_statement(supplier, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
    assert [(l["type"], l["amount"], l["balance"]) for l in statement["lines"]] == [
        ("bill", Decimal("300.00"), Decimal("300.00")), ("payment", Decimal("-120.00"), Decimal("180.00")),
        ("bill", Decimal("40.00"), Decimal("220.00")), ("cancellation", Decimal("-40.00"), Decimal("180.00"))]
    assert statement["closing_balance"] == ap_balance()
    later = supplier_statement(supplier, start_date=date(2026, 1, 21), end_date=date(2026, 1, 31))
    assert later["opening_balance"] == Decimal("180.00")


def test_cancellation(supplier, clerk, approver, posted):
    record_supplier_payment(supplier, amount="10.00", reference="SP-1", paid_on=date(2026, 1, 20),
                            allocations=[{"bill": posted, "amount": "10.00", "reference": "SP-1-A"}])
    with pytest.raises(ValidationError, match="allocated payments"):
        cancel_bill(posted, cancellation_date=date(2026, 1, 21))
    approved = approve_bill(draft(supplier, clerk, number="SUP-2"), actor=approver)
    assert cancel_bill(approved, cancellation_date=DAY).cancellation_entry is None
    unpaid = post_bill(approve_bill(draft(supplier, clerk, number="SUP-3"), actor=approver))
    cancelled = cancel_bill(unpaid, cancellation_date=date(2026, 1, 6))
    reversal = cancelled.cancellation_entry
    assert reversal.reversal_of == unpaid.journal_entry
    assert {(l.account.code, l.debit, l.credit) for l in reversal.lines.all()} == {
        ("5000", 0, Decimal("200.00")), ("5100", 0, Decimal("100.00")), ("2000", Decimal("300.00"), 0)}
    with pytest.raises(ValidationError, match="already cancelled"):
        cancel_bill(cancelled, cancellation_date=date(2026, 1, 6))


def test_supplier_payments_match_bank_lines(supplier, posted):
    payment = record_supplier_payment(supplier, amount="300.00", reference="SP-1", paid_on=date(2026, 1, 20),
                                      allocations=[{"bill": posted, "amount": "300.00", "reference": "SP-1-A"}])
    statement = import_bank_statement(reference="BANK-1", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
                                      opening_balance="0.00", closing_balance="-300.00",
                                      lines=[{"booked_on": date(2026, 1, 21), "amount": "-300.00"}])
    assert statement.lines.get().match.journal_line.entry == payment.journal_entry


def test_database_rejects_bill_changes_that_bypass_the_services(supplier, clerk, approver, posted):
    bill_draft = draft(supplier, clerk, number="SUP-2")
    with pytest.raises(IntegrityError):  # A draft cannot skip approval.
        raw("UPDATE accounting_bill SET status = 'posted', posted_total = 300, journal_entry_id = %s,"
            " approved_by_id = %s, approved_at = CURRENT_TIMESTAMP WHERE id = %s",
            db_id(posted.journal_entry), db_id(approver), db_id(bill_draft))
    with pytest.raises(IntegrityError):  # The creator cannot approve.
        raw("UPDATE accounting_bill SET status = 'approved', approved_by_id = %s, approved_at = CURRENT_TIMESTAMP"
            " WHERE id = %s", db_id(clerk), db_id(bill_draft))
    for sql in ("UPDATE accounting_bill SET due_date = '2026-12-31' WHERE id = %s",
                "UPDATE accounting_bill SET status = 'cancelled' WHERE id = %s",
                "DELETE FROM accounting_bill WHERE id = %s",
                "UPDATE accounting_billline SET unit_price = 1 WHERE bill_id = %s",
                "DELETE FROM accounting_billline WHERE bill_id = %s"):
        with pytest.raises(IntegrityError):
            raw(sql, db_id(posted))
    with pytest.raises(IntegrityError):
        raw("INSERT INTO accounting_bill (id, created_at, updated_at, supplier_id, number, bill_date, due_date,"
            " currency, status) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, 'RAW', %s, %s, 'EUR',"
            " 'posted')", new_id(), db_id(supplier), DAY, DAY)
    payment = record_supplier_payment(supplier, amount="50.00", reference="SP-1", paid_on=DAY)
    allocation = ("INSERT INTO accounting_supplierpaymentallocation (id, created_at, updated_at, payment_id,"
                  " bill_id, reference, amount, allocated_on) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,"
                  " %s, %s, %s, %s, %s)")
    with pytest.raises(IntegrityError):  # More than the payment's unapplied amount.
        raw(allocation, new_id(), db_id(payment), db_id(posted), "RAW-A", "60.00", DAY)
    with pytest.raises(IntegrityError):  # A draft bill.
        raw(allocation, new_id(), db_id(payment), db_id(bill_draft), "RAW-B", "10.00", DAY)
    raw(allocation, new_id(), db_id(payment), db_id(posted), "RAW-C", "50.00", DAY)
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_supplierpayment SET amount = 1 WHERE id = %s", db_id(payment))
    with pytest.raises(IntegrityError):  # The entry of another document.
        raw("INSERT INTO accounting_supplierpayment (id, created_at, updated_at, supplier_id, reference, amount,"
            " currency, paid_on, journal_entry_id) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, 'RAW-P',"
            " 300, 'EUR', %s, %s)", new_id(), db_id(supplier), DAY, db_id(posted.journal_entry))
    with pytest.raises(ValidationError):
        Bill.objects.all().update(number="X")


AP_PERMISSIONS = ["view_supplier", "add_supplier", "change_supplier", "view_bill", "add_bill", "change_bill",
                  "delete_bill", "post_bill", "cancel_bill", "view_supplierpayment", "add_supplierpayment",
                  "view_supplierpaymentallocation", "add_supplierpaymentallocation"]


def api_client(user, *permissions):
    grant(user, *permissions)
    client = APIClient()
    client.force_authenticate(get_user_model().objects.get(pk=user.pk))
    return client


def test_api_workflow(clerk, approver):
    clerk_api = api_client(clerk, *AP_PERMISSIONS)
    supplier = clerk_api.post(BASE + "suppliers/", {"name": "Cloud Host", "email": "ap@cloud.example"},
                              format="json")
    assert supplier.status_code == 201
    body = {"supplier": supplier.data["id"], "number": "CH-1", "bill_date": "2026-01-05", "due_date": "2026-02-04",
            "lines": [{"account": "5000", "description": "Hosting", "quantity": "1", "unit_price": "80.00"}]}
    created = clerk_api.post(BASE + "bills/", body, format="json")
    assert created.status_code == 201 and created.data["lines"][0]["account"] == "5000"
    bill_url = f"{BASE}bills/{created.data['id']}/"
    assert clerk_api.post(bill_url + "approve/").status_code == 403  # No approve_bill permission.
    approver_api = api_client(approver, "view_bill", "view_supplierpaymentallocation", "approve_bill")
    approved = approver_api.post(bill_url + "approve/")
    assert approved.status_code == 200 and approved.data["approved_by"] == approver.pk
    assert clerk_api.put(bill_url, body, format="json").status_code == 400
    posted = clerk_api.post(bill_url + "post/")
    assert (posted.status_code, posted.data["status"], posted.data["open_balance"]) == (200, "posted", "80.00")

    payment = {"supplier": supplier.data["id"], "reference": "CH-PAY", "amount": "50.00", "paid_on": "2026-01-10",
               "allocations": [{"bill": created.data["id"], "amount": "50.00", "reference": "CH-PAY-A"}]}
    paid = clerk_api.post(BASE + "supplier-payments/", payment, format="json")
    assert (paid.status_code, paid.data["unapplied_amount"]) == (201, "0.00")
    assert clerk_api.post(BASE + "supplier-payments/", payment, format="json").status_code == 200
    assert clerk_api.post(BASE + "supplier-payments/", {**payment, "amount": 50}, format="json").status_code == 400

    aging = clerk_api.get(BASE + "ap-aging/", {"as_of": "2026-01-31"})
    assert aging.status_code == 200 and aging.data["currencies"]["EUR"]["net"] == "30.00"
    statement = clerk_api.get(f"{BASE}suppliers/{supplier.data['id']}/statement/",
                              {"start_date": "2026-01-01", "end_date": "2026-01-31"})
    assert statement.status_code == 200 and statement.data["closing_balance"] == "30.00"
    actors = dict(AuditEvent.objects.filter(object_id=created.data["id"]).values_list("action", "actor__email"))
    assert actors == {"bill.drafted": clerk.email, "bill.approved": approver.email, "bill.posted": clerk.email}
