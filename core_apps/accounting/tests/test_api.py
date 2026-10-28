from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from rest_framework.test import APIClient

from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine
from core_apps.accounting.services import post_journal_entry

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"
ENDPOINTS = ["accounts/", "entries/", "general-ledger/", "trial-balance/"]


@pytest.fixture
def user():
    return get_user_model().objects.create(
        email="reader@example.com",
        username="reader",
        id_no=10001,
        first_name="Ledger",
        last_name="Reader",
        security_question="favorite_color",
        security_answer="blue",
    )


@pytest.fixture
def client(user):
    group = Group.objects.create(name="Accountant")
    group.permissions.set(
        Permission.objects.filter(
            content_type__app_label="accounting", codename__startswith="view_"
        )
    )
    user.groups.add(group)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def entry_factory():
    def create(amount="25.00", currency="EUR", day=date(2026, 1, 10), posted=True):
        entry = JournalEntry.objects.create(entry_date=day, memo="Test receipt")
        JournalLine.objects.create(
            entry=entry,
            account=ChartOfAccount.objects.get(code="1000"),
            debit=Decimal(amount),
            currency=currency,
        )
        JournalLine.objects.create(
            entry=entry,
            account=ChartOfAccount.objects.get(code="4000"),
            credit=Decimal(amount),
            currency=currency,
        )
        if posted:
            post_journal_entry(entry)
        return entry

    return create


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_anonymous_cannot_read_ledger(endpoint):
    assert APIClient().get(BASE + endpoint).status_code in (401, 403)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_customer_and_staff_without_permissions_denied(user, endpoint):
    client = APIClient()
    client.force_authenticate(user)
    assert client.get(BASE + endpoint).status_code == 403
    user.is_staff = True
    user.save()
    assert client.get(BASE + endpoint).status_code == 403


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_read_permissions_grant_access(client, endpoint):
    assert client.get(BASE + endpoint).status_code == 200


def test_inactive_accountant_denied(client, user):
    user.is_active = False
    user.save()
    assert client.get(BASE + "trial-balance/").status_code == 403


def test_accounts_filter_search_and_pagination(client):
    response = client.get(BASE + "accounts/", {"account_type": "asset", "page_size": 2})
    assert response.status_code == 200
    assert response.data["count"] == 3
    assert len(response.data["results"]) == 2
    assert response.data["next"] is not None
    response = client.get(BASE + "accounts/", {"search": "Stripe"})
    assert response.data["count"] == 1
    assert response.data["results"][0]["code"] == "1010"


def test_entries_detail_and_status_filter(client, entry_factory):
    entry = entry_factory()
    entry_factory(posted=False)
    response = client.get(BASE + "entries/", {"status": "posted"})
    assert response.data["count"] == 1
    response = client.get(BASE + f"entries/{entry.pk}/")
    assert response.status_code == 200
    assert len(response.data["lines"]) == 2
    assert (
        client.patch(BASE + f"entries/{entry.pk}/", {"memo": "edit"}).status_code == 405
    )
    assert client.delete(BASE + f"entries/{entry.pk}/").status_code == 405
    assert client.post(BASE + "entries/", {}).status_code == 405


def test_entry_detail_requires_permissions(user, entry_factory):
    entry = entry_factory()
    client = APIClient()
    client.force_authenticate(user)
    assert client.get(BASE + f"entries/{entry.pk}/").status_code == 403


def test_trial_balance_excludes_drafts(client, entry_factory):
    entry_factory()
    entry_factory("10.25")
    entry_factory("99.00", posted=False)
    response = client.get(BASE + "trial-balance/")
    assert response.status_code == 200
    groups = {row["currency"]: row for row in response.data["currencies"]}
    assert list(groups) == ["EUR"]
    assert groups["EUR"]["debit_total"] == groups["EUR"]["credit_total"] == "35.25"
    assert groups["EUR"]["balanced"]
    assert groups["EUR"]["accounts"][0]["code"] == "1000"


@pytest.mark.parametrize("currency", ["USD", "GBP"])
def test_journal_lines_must_use_the_base_currency(currency):
    entry = JournalEntry.objects.create()
    with pytest.raises(ValidationError):
        JournalLine.objects.create(entry=entry, account=ChartOfAccount.objects.get(code="1000"),
                                   debit=Decimal("1"), currency=currency)
    line = JournalLine.objects.create(entry=entry, account=ChartOfAccount.objects.get(code="1000"),
                                      debit=Decimal("1"))
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE accounting_journalline SET currency = %s WHERE id = %s",
                       [currency, JournalLine._meta.pk.get_db_prep_value(line.pk, connection)])


def test_trial_balance_uses_net_account_balances(client, entry_factory):
    entry_factory("25.00")
    correction = JournalEntry.objects.create()
    JournalLine.objects.create(
        entry=correction,
        account=ChartOfAccount.objects.get(code="1000"),
        credit=Decimal("5"),
    )
    JournalLine.objects.create(
        entry=correction,
        account=ChartOfAccount.objects.get(code="4000"),
        debit=Decimal("5"),
    )
    post_journal_entry(correction)
    result = client.get(BASE + "trial-balance/").data["currencies"][0]
    assert result["debit_total"] == result["credit_total"] == "20.00"


def test_report_filters_and_ledger_pagination(client, entry_factory):
    entry = entry_factory()
    entry_factory(day=date(2026, 1, 11))
    entry_factory(posted=False)
    params = {"start_date": "2026-01-10", "end_date": "2026-01-10", "currency": "EUR"}
    response = client.get(BASE + "general-ledger/", {**params, "page_size": 1})
    assert response.data["count"] == 2
    assert len(response.data["results"]) == 1
    assert str(response.data["results"][0]["entry"]) == str(entry.pk)
    report = client.get(BASE + "trial-balance/", params).data["currencies"][0]
    assert report["debit_total"] == "25.00"
    cash = ChartOfAccount.objects.get(code="1000")
    response = client.get(BASE + "general-ledger/", {**params, "account": str(cash.pk)})
    assert response.data["count"] == 1


@pytest.mark.parametrize("endpoint", ["general-ledger/", "trial-balance/"])
@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "bad-date"},
        {"start_date": "2026-02-01", "end_date": "2026-01-01"},
        {"account": "bad-uuid"},
        {"currency": "invalid"},
    ],
)
def test_invalid_report_filters_rejected(client, endpoint, params):
    assert client.get(BASE + endpoint, params).status_code == 400


def test_empty_trial_balance(client):
    assert client.get(BASE + "trial-balance/").data == {"currencies": []}
