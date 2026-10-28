from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine
from core_apps.accounting.services import post_journal_entry


@pytest.fixture
def ledger_accounts():
    cash = ChartOfAccount.objects.create(
        code="1300", name="Cash", account_type=ChartOfAccount.AccountType.ASSET
    )
    revenue = ChartOfAccount.objects.create(
        code="4300", name="Revenue", account_type=ChartOfAccount.AccountType.INCOME
    )
    return cash, revenue


@pytest.mark.django_db
def test_posting_balanced_entry_marks_it_posted(ledger_accounts):
    cash, revenue = ledger_accounts
    entry = JournalEntry.objects.create(memo="Invoice payment")
    JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("25.00"))
    JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("25.00"))

    posted_entry = post_journal_entry(entry)

    assert posted_entry.status == JournalEntry.Status.POSTED
    assert posted_entry.posted_at is not None


@pytest.mark.django_db
def test_unbalanced_entry_remains_draft(ledger_accounts):
    cash, revenue = ledger_accounts
    entry = JournalEntry.objects.create(memo="Unbalanced entry")
    JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("25.00"))
    JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("20.00"))

    with pytest.raises(ValidationError, match="must balance"):
        post_journal_entry(entry)

    entry.refresh_from_db()
    assert entry.status == JournalEntry.Status.DRAFT


@pytest.mark.django_db
def test_posted_entry_cannot_be_posted_twice(ledger_accounts):
    cash, revenue = ledger_accounts
    entry = JournalEntry.objects.create(memo="Already posted")
    JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("25.00"))
    JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("25.00"))
    post_journal_entry(entry)

    with pytest.raises(ValidationError, match="Only draft"):
        post_journal_entry(entry)


@pytest.mark.django_db
def test_posted_entry_and_lines_are_immutable(ledger_accounts):
    cash, revenue = ledger_accounts
    entry = JournalEntry.objects.create(memo="Immutable entry")
    cash_line = JournalLine.objects.create(
        entry=entry, account=cash, debit=Decimal("25.00")
    )
    JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("25.00"))
    post_journal_entry(entry)

    entry.memo = "Changed"
    with pytest.raises(ValidationError, match="immutable"):
        entry.save()

    cash_line.description = "Changed"
    with pytest.raises(ValidationError, match="immutable"):
        cash_line.save()

    with pytest.raises(ValidationError, match="cannot be deleted"):
        cash_line.delete()
