from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from core_apps.accounting.financial_reports import balance_sheet, profit_and_loss
from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine, PeriodClose
from core_apps.accounting.period_close import close_period
from core_apps.accounting.services import cancel_invoice, post_invoice, post_lines
from .test_ar_integrity import db_id, raw
from .test_ar_workflow import invoice
from .test_ar_write_api import grant
from .test_api import client, user

pytestmark = pytest.mark.django_db
ZERO = Decimal("0")
YEAR_END = date(2026, 12, 31)


def expense(amount, day):
    amount = Decimal(amount)
    return post_lines([("5000", amount, ZERO), ("1000", ZERO, amount)], currency="EUR", source_type="",
                      source_id=None, entry_date=day, memo="Expense")


def test_nothing_can_be_posted_into_a_closed_period():
    expense("10", date(2026, 1, 15))
    late = JournalEntry.objects.create(entry_date=date(2026, 1, 20), memo="Late")
    accounts = {a.code: a for a in ChartOfAccount.objects.filter(code__in=["5000", "1000"])}
    JournalLine.objects.create(entry=late, account=accounts["5000"], debit=5)
    JournalLine.objects.create(entry=late, account=accounts["1000"], credit=5)
    PeriodClose.objects.create(closed_through=date(2026, 1, 31))  # A close that left a draft behind.
    with pytest.raises(ValidationError, match="closed through 2026-01-31"):
        expense("5", date(2026, 1, 31))
    expense("5", date(2026, 2, 1))
    with pytest.raises(IntegrityError):  # The balanced draft cannot be posted past the service either.
        raw("UPDATE accounting_journalentry SET status = 'posted', posted_at = CURRENT_TIMESTAMP WHERE id = %s",
            db_id(late))


def test_closes_move_forward_only_and_need_posted_drafts():
    close_period(through=date(2026, 1, 31))
    for through in (date(2026, 1, 31), date(2026, 1, 15)):
        with pytest.raises(ValidationError, match="already closed"):
            close_period(through=through)
    JournalEntry.objects.create(entry_date=date(2026, 2, 10), memo="Unposted")
    with pytest.raises(ValidationError, match="draft entries"):
        close_period(through=date(2026, 2, 28))
    close = PeriodClose.objects.get()
    for sql in ("UPDATE accounting_periodclose SET closed_through = '2025-12-31' WHERE id = %s",
                "DELETE FROM accounting_periodclose WHERE id = %s"):
        with pytest.raises(IntegrityError):
            raw(sql, db_id(close))


def test_year_end_moves_profit_to_retained_earnings(invoice):
    post_invoice(invoice)  # Sales 100 on 1 January.
    expense("30", date(2026, 6, 30))
    close = close_period(through=YEAR_END, transfer_earnings=True)
    entry = close.closing_entry
    assert (entry.entry_date, entry.source_type) == (YEAR_END, "period_close")
    assert {(l.account.code, l.debit, l.credit) for l in entry.lines.all()} == {
        ("4000", Decimal("100.00"), 0), ("5000", 0, Decimal("30.00")), ("3000", 0, Decimal("70.00"))}
    sheet = balance_sheet(as_of=YEAR_END)
    assert sheet["equity"]["current_earnings"] == 0 and sheet["balanced"]
    assert {a["code"]: a["balance"] for a in sheet["equity"]["accounts"]} == {"3000": Decimal("70.00")}
    year = profit_and_loss(start_date=date(2026, 1, 1), end_date=YEAR_END)
    assert year["net_income"] == Decimal("70.00")  # The closing entry is not income or expense.

    expense("10", date(2027, 1, 5))
    close_period(through=date(2027, 12, 31), transfer_earnings=True)
    sheet = balance_sheet(as_of=date(2027, 12, 31))
    assert {a["code"]: a["balance"] for a in sheet["equity"]["accounts"]} == {"3000": Decimal("60.00")}


def test_year_end_without_activity_posts_no_entry():
    assert close_period(through=YEAR_END, transfer_earnings=True).closing_entry is None


def test_corrections_go_to_the_next_open_period(invoice):
    posted = post_invoice(invoice)
    close_period(through=date(2026, 1, 31))
    with pytest.raises(ValidationError, match="closed"):
        cancel_invoice(posted, cancellation_date=date(2026, 1, 20))
    cancelled = cancel_invoice(posted, cancellation_date=date(2026, 2, 1))
    assert cancelled.cancellation_entry.entry_date == date(2026, 2, 1)


def test_period_api(client, user):
    assert client.get("/api/v1/accounting/periods/").status_code == 200
    body = {"through": "2026-01-31", "transfer_earnings": False}
    assert client.post("/api/v1/accounting/periods/", body, format="json").status_code == 403
    grant(user, "add_periodclose", "add_journalentry")
    client.force_authenticate(type(user).objects.get(pk=user.pk))
    created = client.post("/api/v1/accounting/periods/", body, format="json")
    assert created.status_code == 201 and created.data["closed_through"] == "2026-01-31"
    assert client.post("/api/v1/accounting/periods/", body, format="json").status_code == 400
