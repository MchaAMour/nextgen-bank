from datetime import date

import pytest
from rest_framework.test import APIClient

from .test_api import client, user
from .test_ar_workflow import invoice
from core_apps.accounting.services import post_invoice, receive_invoice_payment

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"


@pytest.mark.parametrize("endpoint", ["customers/", "invoices/", "payments/", "credit-notes/", "ar-aging/"])
def test_ar_requires_permission(endpoint, user):
    api = APIClient()
    assert api.get(BASE + endpoint).status_code in (401, 403)
    api.force_authenticate(user)
    assert api.get(BASE + endpoint).status_code == 403


def test_invoice_balances_and_payments(client, invoice):
    invoice = post_invoice(invoice)
    receive_invoice_payment(invoice, amount="30", reference="AR-API", received_on=date(2026, 2, 1))
    response = client.get(BASE + "invoices/", {"status": "posted", "currency": "USD"})
    assert response.status_code == 200
    assert response.data["count"] == 1
    row = response.data["results"][0]
    assert row["open_balance"] == "70.00"
    assert row["posted_total"] == "100.00"
    response = client.get(BASE + "payments/", {"customer": str(invoice.customer.pk)})
    payment = response.data["results"][0]
    assert (payment["amount"], payment["unapplied_amount"]) == ("30.00", "0.00")
    assert payment["allocations"][0]["invoice"] == invoice.pk
    assert client.get(BASE + "customers/").data["count"] == 1
    assert client.post(BASE + "invoices/", {}).status_code == 403


def test_historical_ar_api(client, invoice):
    invoice = post_invoice(invoice)
    receive_invoice_payment(invoice, amount="30", reference="AR-HISTORY", received_on=date(2026, 2, 1))
    response = client.get(BASE + "ar-aging/", {"as_of": "2026-01-31"})
    assert response.status_code == 200
    assert response.data["currencies"]["USD"]["current"] == "100.00"
    response = client.get(BASE + "ar-aging/", {"as_of": "2026-02-01"})
    assert response.data["currencies"]["USD"]["1_30"] == "70.00"
    assert client.get(BASE + "ar-aging/", {"as_of": "invalid"}).status_code == 400
