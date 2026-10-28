from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from core_apps.accounting.models import Customer, Invoice, InvoiceLine, JournalEntry
from core_apps.accounting.services import create_draft_invoice

pytestmark = pytest.mark.django_db


@pytest.fixture
def draft_data():
    return dict(customer=Customer.objects.create(name="Example customer"), number="INV-001",
                issue_date=date(2026, 1, 1), due_date=date(2026, 1, 31),
                lines=[dict(description="Consulting", quantity="1.500", unit_price="10.01")])


def test_draft_total_rounds_lines_without_posting(draft_data):
    draft_data["lines"] *= 2
    invoice = create_draft_invoice(**draft_data)
    assert invoice.total == Decimal("30.04")
    assert invoice.lines.count() == 2
    assert not JournalEntry.objects.exists()


@pytest.mark.parametrize("field,value", [("quantity", "0"), ("quantity", "-1"),
                                         ("unit_price", "-1"), ("unit_price", "1.001"),
                                         ("unit_price", 1.1), ("quantity", True)])
def test_invalid_line_rolls_back_whole_document(draft_data, field, value):
    draft_data["lines"].append(dict(description="Invalid", quantity="1", unit_price="2"))
    draft_data["lines"][1][field] = value
    with pytest.raises(ValidationError):
        create_draft_invoice(**draft_data)
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


@pytest.mark.parametrize("change", ["dates", "empty", "currency", "inactive"])
def test_invalid_document_rejected(draft_data, change):
    if change == "dates":
        draft_data["due_date"] = date(2025, 12, 31)
    elif change == "empty":
        draft_data["lines"] = []
    elif change == "currency":
        draft_data["currency"] = "JPY"
    else:
        draft_data["customer"].is_active = False
        draft_data["customer"].save()
    with pytest.raises(ValidationError):
        create_draft_invoice(**draft_data)
    assert not Invoice.objects.exists()


def test_duplicate_number_rejected_and_customer_protected(draft_data):
    create_draft_invoice(**draft_data)
    with pytest.raises(ValidationError):
        create_draft_invoice(**draft_data)
    assert Invoice.objects.count() == 1
    with pytest.raises(ProtectedError):
        draft_data["customer"].delete()
