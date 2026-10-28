from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.db import IntegrityError
from django.db.models import Sum
from rest_framework.test import APIClient

from core_apps.accounting.models import Customer, Invoice, InvoiceReceipt, JournalEntry, JournalLine
from core_apps.accounting.services import post_invoice, receive_invoice_payment
from .test_api import client, user
from .test_ar_workflow import invoice

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"
WRITE_PERMISSIONS = ["add_customer", "change_customer", "add_invoice", "change_invoice",
                     "delete_invoice", "post_invoice", "cancel_invoice", "add_invoicereceipt"]


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounting",
                                                         codename__in=codenames))


@pytest.fixture
def writer(client, user):
    grant(user, *WRITE_PERMISSIONS)
    return client


def draft_payload(customer, **changes):
    return {"customer": str(customer.pk), "number": "API-1", "issue_date": "2026-01-01",
            "due_date": "2026-01-31", "currency": "USD",
            "lines": [{"description": "Work", "quantity": "2", "unit_price": "50.00"}], **changes}


def ledger_balanced():
    totals = JournalLine.objects.aggregate(debits=Sum("debit"), credits=Sum("credit"))
    return totals["debits"] == totals["credits"]


def test_invoice_lifecycle_through_api(writer, user):
    customer = writer.post(BASE + "customers/", {"name": "API customer", "email": "ar@example.com"},
                           format="json")
    assert customer.status_code == 201
    customer = Customer.objects.get(pk=customer.data["id"])
    draft = writer.post(BASE + "invoices/", draft_payload(customer), format="json")
    assert draft.status_code == 201
    assert (draft.data["status"], draft.data["total"]) == ("draft", "100.00")
    assert not JournalEntry.objects.exists()

    url = f"{BASE}invoices/{draft.data['id']}/"
    lines = [{"description": "Work", "quantity": "1.5", "unit_price": "100.00"}]
    replaced = writer.put(url, draft_payload(customer, number="API-1A", lines=lines), format="json")
    assert replaced.status_code == 200
    assert (replaced.data["number"], replaced.data["total"], len(replaced.data["lines"])) == (
        "API-1A", "150.00", 1)

    posted = writer.post(url + "post/")
    assert posted.status_code == 200
    assert (posted.data["status"], posted.data["open_balance"]) == ("posted", "150.00")
    assert JournalEntry.objects.get(pk=posted.data["journal_entry"]).posted_by == user

    receipt = {"invoice": draft.data["id"], "reference": "API-R1", "amount": "40.00",
               "received_on": "2026-01-15"}
    assert writer.post(BASE + "receipts/", receipt, format="json").status_code == 201
    assert writer.post(BASE + "receipts/", receipt, format="json").status_code == 200
    assert writer.post(BASE + "receipts/", {**receipt, "amount": "41.00"}, format="json").status_code == 400
    over = {**receipt, "reference": "API-R2", "amount": "110.01"}
    assert writer.post(BASE + "receipts/", over, format="json").status_code == 400
    assert InvoiceReceipt.objects.count() == 1
    assert writer.get(url).data["open_balance"] == "110.00"
    assert JournalEntry.objects.count() == 2 and ledger_balanced()


@pytest.mark.parametrize("method,path,permission", [
    ("post", "customers/", "add_customer"),
    ("patch", "customers/{customer}/", "change_customer"),
    ("post", "invoices/", "add_invoice"),
    ("put", "invoices/{invoice}/", "change_invoice"),
    ("delete", "invoices/{invoice}/", "delete_invoice"),
    ("post", "invoices/{invoice}/post/", "post_invoice"),
    ("post", "invoices/{invoice}/cancel/", "cancel_invoice"),
    ("post", "receipts/", "add_invoicereceipt"),
])
def test_each_write_requires_its_own_permission(client, user, invoice, method, path, permission):
    grant(user, *(p for p in WRITE_PERMISSIONS if p != permission))
    url = BASE + path.format(customer=invoice.customer.pk, invoice=invoice.pk)
    assert getattr(client, method)(url, {}, format="json").status_code == 403
    anonymous = getattr(APIClient(), method)(url, {}, format="json")
    assert anonymous.status_code in (401, 403)
    assert (Invoice.objects.filter(status="draft").count(), Customer.objects.count()) == (1, 1)
    grant(user, permission)
    user = type(user).objects.get(pk=user.pk)  # Clear the cached permission set.
    client.force_authenticate(user)
    assert getattr(client, method)(url, {}, format="json").status_code != 403


def test_unsupported_methods_return_405(writer, invoice):
    assert writer.patch(f"{BASE}invoices/{invoice.pk}/", {}, format="json").status_code == 405
    assert writer.delete(f"{BASE}customers/{invoice.customer.pk}/").status_code == 405
    assert writer.delete(BASE + "receipts/").status_code == 405


@pytest.mark.parametrize("field", ["quantity", "unit_price"])
def test_json_number_amounts_are_rejected(writer, invoice, field):
    lines = [{"description": "Work", "quantity": "1", "unit_price": "10.00", field: 10.5}]
    response = writer.post(BASE + "invoices/", draft_payload(invoice.customer, number="API-F", lines=lines),
                           format="json")
    assert response.status_code == 400
    assert not Invoice.objects.filter(number="API-F").exists()


def test_json_number_receipt_amount_is_rejected(writer, invoice):
    post_invoice(invoice)
    response = writer.post(BASE + "receipts/", {"invoice": str(invoice.pk), "reference": "F",
                           "amount": 10.5, "received_on": "2026-01-02"}, format="json")
    assert response.status_code == 400
    assert not InvoiceReceipt.objects.exists()


@pytest.mark.parametrize("changes", [{"due_date": "2025-12-31"}, {"currency": "JPY"}, {"lines": []},
    {"number": "AR-1"}, {"lines": [{"description": "Bad", "quantity": "0", "unit_price": "1"}]}])
def test_invalid_drafts_are_rejected(writer, invoice, changes):
    response = writer.post(BASE + "invoices/", draft_payload(invoice.customer, **changes), format="json")
    assert response.status_code == 400
    assert Invoice.objects.count() == 1


def test_inactive_customer_cannot_be_invoiced(writer, invoice):
    url = f"{BASE}customers/{invoice.customer.pk}/"
    assert writer.patch(url, {"is_active": False}, format="json").data["is_active"] is False
    response = writer.post(BASE + "invoices/", draft_payload(invoice.customer, number="API-2"), format="json")
    assert response.status_code == 400
    assert writer.put(f"{BASE}invoices/{invoice.pk}/", draft_payload(invoice.customer),
                      format="json").status_code == 400


def test_posted_invoice_cannot_be_replaced_or_deleted(writer, invoice):
    post_invoice(invoice)
    url = f"{BASE}invoices/{invoice.pk}/"
    assert writer.put(url, draft_payload(invoice.customer, number="AR-1"), format="json").status_code == 400
    assert writer.delete(url).status_code == 400
    assert writer.post(url + "post/").status_code == 400
    invoice.refresh_from_db()
    assert (invoice.status, invoice.posted_total, invoice.lines.count()) == ("posted", Decimal("100.00"), 1)


def test_draft_invoice_can_be_deleted(writer, invoice):
    assert writer.delete(f"{BASE}invoices/{invoice.pk}/").status_code == 204
    assert not Invoice.objects.exists()


def test_cancellation_through_api(writer, invoice):
    post_invoice(invoice)
    url = f"{BASE}invoices/{invoice.pk}/cancel/"
    assert writer.post(url, {}, format="json").status_code == 400
    assert writer.post(url, {"cancellation_date": "2025-12-31"}, format="json").status_code == 400
    response = writer.post(url, {"cancellation_date": "2026-02-01"}, format="json")
    assert (response.status_code, response.data["status"]) == (200, "cancelled")
    assert writer.post(url, {"cancellation_date": "2026-02-02"}, format="json").status_code == 400
    ar = JournalLine.objects.filter(account__code="1100").aggregate(d=Sum("debit"), c=Sum("credit"))
    assert ar["d"] == ar["c"] and ledger_balanced()


def test_paid_invoice_cannot_be_cancelled(writer, invoice):
    receive_invoice_payment(post_invoice(invoice), amount="10", reference="PAID", received_on=invoice.issue_date)
    response = writer.post(f"{BASE}invoices/{invoice.pk}/cancel/", {"cancellation_date": "2026-02-01"},
                           format="json")
    assert response.status_code == 400
    assert Invoice.objects.get(pk=invoice.pk).status == "posted"


def test_posting_failure_rolls_back(writer, invoice):
    from django.core.exceptions import ValidationError
    with patch("core_apps.accounting.services.post_journal_entry", side_effect=ValidationError("failure")):
        assert writer.post(f"{BASE}invoices/{invoice.pk}/post/").status_code == 400
    invoice.refresh_from_db()
    assert invoice.status == "draft" and not JournalEntry.objects.exists()


def test_database_conflict_returns_409(writer, invoice):
    post_invoice(invoice)
    with patch("core_apps.accounting.ar_views.receive_invoice_payment", side_effect=IntegrityError):
        response = writer.post(BASE + "receipts/", {"invoice": str(invoice.pk), "reference": "C",
                               "amount": "1.00", "received_on": "2026-01-02"}, format="json")
    assert response.status_code == 409
