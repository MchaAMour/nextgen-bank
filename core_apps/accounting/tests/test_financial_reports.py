from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from core_apps.accounting.bank_reconciliation import import_bank_statement, post_statement_line
from core_apps.accounting.financial_reports import balance_sheet, customer_statement, profit_and_loss
from core_apps.accounting.models import AuditEvent, Customer
from core_apps.accounting.services import (cancel_invoice, create_draft_invoice, issue_credit_note, post_invoice,
    record_customer_payment, refund_payment)
from .test_api import client, user
from .test_ar_integrity import db_id, raw
from .test_ar_write_api import WRITE_PERMISSIONS, grant

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"


def posted_invoice(customer, number, amount, day, actor=None):
    invoice = create_draft_invoice(customer=customer, number=number, issue_date=day, due_date=day,
                                   lines=[dict(description="Work", quantity="1", unit_price=amount)], actor=actor)
    return post_invoice(invoice, actor=actor)


@pytest.fixture
def january():
    """Sales 150 (100 + 80 + 40, less a 40 cancellation and a 30 credit note); bank charges 4.50."""
    customer = Customer.objects.create(name="Statement customer", email="customer@example.com")
    first = posted_invoice(customer, "J-1", "100", date(2026, 1, 1))
    cancelled = posted_invoice(customer, "J-3", "40", date(2026, 1, 5))
    cancel_invoice(cancelled, cancellation_date=date(2026, 1, 6))
    payment = record_customer_payment(customer, amount="150", currency="EUR", reference="P-1",
                                      received_on=date(2026, 1, 10),
                                      allocations=[{"invoice": first, "amount": "100", "reference": "P-1-A"}])
    refund_payment(payment, amount="20", reference="RF-1", refunded_on=date(2026, 1, 12))
    second = posted_invoice(customer, "J-2", "80", date(2026, 1, 15))
    issue_credit_note(second, number="CN-1", amount="30", issued_on=date(2026, 1, 16), reason="Discount")
    statement = import_bank_statement(
        reference="BANK-JAN", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), opening_balance="0.00",
        closing_balance="125.50", lines=[
            {"booked_on": date(2026, 1, 10), "amount": "150.00"},
            {"booked_on": date(2026, 1, 12), "amount": "-20.00"},
            {"booked_on": date(2026, 1, 31), "amount": "-4.50", "description": "Charges"}])
    post_statement_line(statement.lines.get(sequence=3), account_code="5000", memo="Bank charges")
    return customer


def balances(section):
    return {account["code"]: account["balance"] for account in section["accounts"]}


def test_balance_sheet_balances_and_shows_current_earnings(january):
    sheet = balance_sheet(as_of=date(2026, 1, 31))
    assert balances(sheet["assets"]) == {"1000": Decimal("125.50"), "1100": Decimal("20.00")}
    assert sheet["assets"]["total"] == Decimal("145.50")
    assert sheet["liabilities"] == {"accounts": [], "total": 0}
    assert sheet["equity"]["current_earnings"] == Decimal("145.50")
    assert sheet["balanced"] is True and sheet["liabilities_and_equity"] == Decimal("145.50")
    earlier = balance_sheet(as_of=date(2026, 1, 9))
    assert balances(earlier["assets"]) == {"1100": Decimal("100.00")} and earlier["balanced"] is True


def test_profit_and_loss_for_a_period(january):
    whole = profit_and_loss(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
    assert balances(whole["income"]) == {"4000": Decimal("150.00")}
    assert balances(whole["expenses"]) == {"5000": Decimal("4.50")}
    assert whole["net_income"] == balance_sheet(as_of=date(2026, 1, 31))["equity"]["current_earnings"]
    early = profit_and_loss(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10))
    assert early["net_income"] == Decimal("100.00")  # The 40 invoice and its cancellation offset.
    with pytest.raises(ValidationError):
        profit_and_loss(start_date=date(2026, 2, 1), end_date=date(2026, 1, 1))


def test_customer_statement_running_balance_matches_the_ar_ledger(january):
    full = customer_statement(january, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
    assert full["opening_balance"] == 0 and full["closing_balance"] == Decimal("20.00")
    assert [(line["type"], line["reference"], line["amount"]) for line in full["lines"]] == [
        ("invoice", "J-1", Decimal("100.00")), ("invoice", "J-3", Decimal("40.00")),
        ("cancellation", "J-3", Decimal("-40.00")), ("payment", "P-1", Decimal("-150.00")),
        ("refund", "RF-1", Decimal("20.00")), ("invoice", "J-2", Decimal("80.00")),
        ("credit_note", "CN-1", Decimal("-30.00"))]
    assert full["closing_balance"] == balances(balance_sheet(as_of=date(2026, 1, 31))["assets"])["1100"]
    later = customer_statement(january, start_date=date(2026, 1, 11), end_date=date(2026, 1, 31))
    assert later["opening_balance"] == Decimal("-50.00")  # Customer credit after the payment.
    assert [line["balance"] for line in later["lines"]] == [Decimal("-30.00"), Decimal("50.00"), Decimal("20.00")]


def test_services_record_audit_events_once(january):
    actions = list(AuditEvent.objects.order_by("created_at").values_list("action", flat=True))
    for action in ["invoice.drafted", "invoice.posted", "invoice.cancelled", "payment.recorded",
                   "payment.allocated", "payment.refunded", "credit_note.issued", "bank_statement.imported",
                   "bank_line.matched", "journal_entry.posted"]:
        assert action in actions
    posted = AuditEvent.objects.filter(action="journal_entry.posted")
    # Invoices J-1, J-3, J-2, the cancellation, payment, refund, credit note and bank charges.
    assert posted.count() == 8
    count = AuditEvent.objects.count()
    record_customer_payment(january, amount="150", currency="EUR", reference="P-1", received_on=date(2026, 1, 10))
    assert AuditEvent.objects.count() == count  # A replay records nothing.


def test_audit_events_are_immutable(january):
    event = AuditEvent.objects.first()
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_auditevent SET summary = 'changed' WHERE id = %s", db_id(event))
    with pytest.raises(IntegrityError):
        raw("DELETE FROM accounting_auditevent WHERE id = %s", db_id(event))
    with pytest.raises(ValidationError):
        AuditEvent.objects.all().delete()
    event.summary = "changed"
    with pytest.raises(ValidationError):
        event.save()


def test_report_apis(client, january):
    sheet = client.get(BASE + "balance-sheet/", {"as_of": "2026-01-31"})
    assert sheet.status_code == 200 and sheet.data["assets"]["total"] == "145.50" and sheet.data["balanced"]
    pnl = client.get(BASE + "profit-and-loss/", {"start_date": "2026-01-01", "end_date": "2026-01-31"})
    assert pnl.status_code == 200 and pnl.data["net_income"] == "145.50"
    assert client.get(BASE + "profit-and-loss/").status_code == 400
    statement = client.get(f"{BASE}customers/{january.pk}/statement/",
                           {"start_date": "2026-01-11", "end_date": "2026-01-31"})
    assert statement.status_code == 200
    assert (statement.data["opening_balance"], statement.data["closing_balance"]) == ("-50.00", "20.00")
    audit = client.get(BASE + "audit-events/", {"action": "credit_note.issued"})
    assert audit.status_code == 200 and audit.data["count"] == 1


def test_api_writes_record_the_requesting_user(client, user):
    grant(user, *WRITE_PERMISSIONS)
    user = type(user).objects.get(pk=user.pk)
    client.force_authenticate(user)
    created = client.post(BASE + "customers/", {"name": "Audited", "email": "a@example.com"}, format="json")
    assert created.status_code == 201
    client.patch(f"{BASE}customers/{created.data['id']}/", {"is_active": False}, format="json")
    events = client.get(BASE + "audit-events/", {"object_id": created.data["id"]}).data["results"]
    assert [(e["action"], e["actor_email"]) for e in events] == [
        ("customer.updated", user.email), ("customer.created", user.email)]
    assert events[0]["details"]["is_active"] is False


def test_reports_need_permissions(user):
    from rest_framework.test import APIClient
    anonymous = APIClient()
    assert anonymous.get(BASE + "balance-sheet/").status_code in (401, 403)
    plain = APIClient()
    plain.force_authenticate(user)
    for url in ("balance-sheet/", "audit-events/", "profit-and-loss/"):
        assert plain.get(BASE + url).status_code == 403
