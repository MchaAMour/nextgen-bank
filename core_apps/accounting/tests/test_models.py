from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine


@pytest.mark.django_db
def test_starter_chart_is_available_after_migrations():
    assert set(
        ChartOfAccount.objects.filter(
            code__in=["1000", "1010", "1100", "2000", "2100", "3000", "4000", "5000"]
        ).values_list("code", flat=True)
    ) == {"1000", "1010", "1100", "2000", "2100", "3000", "4000", "5000"}


@pytest.mark.django_db
def test_chart_account_assigns_normal_balance_from_its_type():
    account = ChartOfAccount.objects.create(
        code="1200", name="Test asset", account_type=ChartOfAccount.AccountType.ASSET
    )

    assert account.normal_balance == ChartOfAccount.NormalBalance.DEBIT


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("debit", "credit"),
    [(Decimal("0.00"), Decimal("0.00")), (Decimal("10.00"), Decimal("10.00"))],
)
def test_journal_line_requires_exactly_one_amount(debit, credit):
    account = ChartOfAccount.objects.create(
        code="1201", name="Line account", account_type=ChartOfAccount.AccountType.ASSET
    )
    entry = JournalEntry.objects.create(memo="Line validation")

    with pytest.raises(ValidationError):
        JournalLine.objects.create(entry=entry, account=account, debit=debit, credit=credit)


def test_base_currency_setting_must_match_ledger(settings):
    from core_apps.accounting.checks import base_currency_matches_ledger
    assert base_currency_matches_ledger(None) == []
    settings.ACCOUNTING_BASE_CURRENCY = "USD"
    assert [error.id for error in base_currency_matches_ledger(None)] == ["accounting.E001"]
