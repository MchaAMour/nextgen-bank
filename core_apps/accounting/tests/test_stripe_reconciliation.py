import time
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.exceptions import ValidationError
from django.db.models import Sum

from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine, PaymentRefund, StripeBalanceTransaction
from core_apps.accounting.services import post_invoice, record_customer_payment, refund_payment
from core_apps.accounting.stripe_reconciliation import (clearing_balance, reconciliation_report,
    sync_balance_transactions)
from .test_api import client, user
from .test_ar_workflow import invoice
from .test_ar_write_api import grant

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/stripe/"
JAN_21 = int(datetime(2026, 1, 21, 12, tzinfo=dt_timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def stripe_settings(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_unit"


@pytest.fixture
def stripe_api():
    """Stands in for StripeClient: `listed` is what the balance transaction list returns."""
    client = MagicMock()
    client.listed = []
    client.v1.balance_transactions.list.side_effect = lambda params: MagicMock(
        auto_paging_iter=lambda: iter(client.listed))
    client.v1.balance.retrieve.return_value = {"available": [], "pending": []}
    with patch("core_apps.accounting.stripe_reconciliation.stripe_client", return_value=client):
        yield client


def stripe_balance(client, available="0", pending="0"):
    client.v1.balance.retrieve.return_value = {
        "available": [{"amount": int(Decimal(available) * 100), "currency": "eur"},
                      {"amount": 999, "currency": "usd"}],
        "pending": [{"amount": int(Decimal(pending) * 100), "currency": "eur"}]}


def txn(txn_id, category, amount, fee="0", source=None, currency="eur", created=JAN_21):
    amount, fee = int(Decimal(amount) * 100), int(Decimal(fee) * 100)
    return {"id": txn_id, "object": "balance_transaction", "reporting_category": category, "amount": amount,
            "fee": fee, "net": amount - fee, "currency": currency, "status": "pending", "created": created,
            "source": source}


def charge(txn_id="txn_ch", amount="100", fee="3.20", payment_intent="pi_1"):
    return txn(txn_id, "charge", amount, fee, {"id": f"ch_{txn_id}", "object": "charge",
                                               "payment_intent": payment_intent})


@pytest.fixture
def posted(invoice):
    return post_invoice(invoice)


def stripe_payment(invoice, amount="100", allocate="100", payment_intent="pi_1", cash_account="1010"):
    allocations = [{"invoice": invoice, "amount": allocate, "reference": f"A-{payment_intent}"}] if allocate else []
    return record_customer_payment(invoice.customer, amount=amount, currency="EUR",
                                   reference=f"stripe:{payment_intent}", received_on=date(2026, 1, 20),
                                   allocations=allocations, cash_account=cash_account)


def balance(code):
    totals = JournalLine.objects.filter(account__code=code, entry__status__in=JournalEntry.IN_LEDGER).aggregate(
        debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or 0) - (totals["credit"] or 0)


def test_fee_account_is_seeded():
    account = ChartOfAccount.objects.get(code="5100")
    assert (account.account_type, account.normal_balance, account.is_active) == ("expense", "debit", True)


def test_charge_fee_is_posted_once_and_clearing_follows_the_stripe_balance(posted, stripe_api):
    payment = stripe_payment(posted)
    stripe_api.listed = [charge()]
    assert sync_balance_transactions() == {"listed": 1, "posted": 1}
    record = StripeBalanceTransaction.objects.get()
    assert (record.state, record.payment, record.net) == ("posted", payment, Decimal("96.80"))
    lines = {(l.account.code, l.debit, l.credit) for l in record.journal_entry.lines.all()}
    assert lines == {("5100", Decimal("3.20"), 0), ("1010", 0, Decimal("3.20"))}
    assert record.journal_entry.source_type == "stripe_balance_transaction"
    assert record.journal_entry.entry_date == date(2026, 1, 21)
    assert clearing_balance() == Decimal("96.80")

    assert sync_balance_transactions() == {"listed": 1}
    assert StripeBalanceTransaction.objects.count() == 1 and balance("5100") == Decimal("3.20")
    created_gte = stripe_api.v1.balance_transactions.list.call_args.args[0]["created"]["gte"]
    assert created_gte == JAN_21 - 86400


def test_charge_waits_for_its_webhook_payment(posted, stripe_api):
    stripe_api.listed = [charge()]
    assert sync_balance_transactions() == {"listed": 1, "needs_review": 1}
    record = StripeBalanceTransaction.objects.get()
    assert "webhook may not be processed yet" in record.review_reason
    assert balance("5100") == 0
    stripe_payment(posted)
    assert sync_balance_transactions()["posted"] == 1
    record.refresh_from_db()
    assert (record.state, record.review_reason) == ("posted", "")


@pytest.mark.parametrize("payment_kwargs, reason", [
    ({"amount": "90", "allocate": "90"}, "Stripe charged 100.00; the payment records 90"),
    ({"cash_account": "1000"}, "was not recorded in Stripe clearing"),
])
def test_charge_that_does_not_match_the_payment_needs_review(posted, stripe_api, payment_kwargs, reason):
    stripe_payment(posted, **payment_kwargs)
    stripe_api.listed = [charge()]
    sync_balance_transactions()
    record = StripeBalanceTransaction.objects.get()
    assert record.state == "needs_review" and reason in record.review_reason
    assert record.payment is None and balance("5100") == 0


def test_charge_without_fee_is_matched_without_posting(posted, stripe_api):
    payment = stripe_payment(posted)
    stripe_api.listed = [charge(fee="0")]
    assert sync_balance_transactions() == {"listed": 1, "nothing_to_post": 1}
    record = StripeBalanceTransaction.objects.get()
    assert (record.payment, record.journal_entry) == (payment, None)


def test_payout_and_reversal_move_money_between_clearing_and_bank(posted, stripe_api):
    stripe_payment(posted)
    stripe_api.listed = [
        charge(),
        txn("txn_po", "payout", "-96.80", source="po_1"),
        txn("txn_po_fee", "payout", "-50", fee="0.25", source={"id": "po_2", "object": "payout"}),
        txn("txn_po_back", "payout_reversal", "50", source="po_2"),
        txn("txn_fee", "fee", "-1.50", source="fee_1"),
    ]
    assert sync_balance_transactions() == {"listed": 5, "posted": 5}
    assert balance("1010") == Decimal("-1.75")
    assert balance("1000") == Decimal("96.80")
    assert balance("5100") == Decimal("3.20") + Decimal("0.25") + Decimal("1.50")
    stripe_balance(stripe_api, available="-1.75")
    report = reconciliation_report()
    assert report["difference"] == 0 and report["reconciled"] is True
    record = StripeBalanceTransaction.objects.get(stripe_id="txn_po_fee")
    assert record.source_id == "po_2"
    assert {(l.account.code, l.debit, l.credit) for l in record.journal_entry.lines.all()} == {
        ("1000", Decimal("50"), 0), ("5100", Decimal("0.25"), 0), ("1010", 0, Decimal("50.25"))}


def test_stripe_refund_returns_unapplied_credit_from_clearing(posted, stripe_api):
    payment = stripe_payment(posted, amount="100", allocate="60")
    refund_source = {"id": "re_1", "object": "refund", "payment_intent": "pi_1"}
    stripe_api.listed = [charge(), txn("txn_re", "refund", "-40", source=refund_source)]
    assert sync_balance_transactions() == {"listed": 2, "posted": 2}
    refund = PaymentRefund.objects.get()
    assert (refund.payment, refund.amount, refund.reference) == (payment, Decimal("40"), "stripe:re_1")
    assert StripeBalanceTransaction.objects.get(stripe_id="txn_re").refund == refund
    assert {(l.account.code, l.debit, l.credit) for l in refund.journal_entry.lines.all()} == {
        ("1100", Decimal("40"), 0), ("1010", 0, Decimal("40"))}
    payment.refresh_from_db()
    assert payment.unapplied_amount == 0
    assert clearing_balance() == Decimal("56.80")


def test_refund_beyond_unapplied_credit_needs_review_and_posts_nothing(posted, stripe_api):
    stripe_payment(posted)
    refund_source = {"id": "re_1", "object": "refund", "payment_intent": "pi_1"}
    stripe_api.listed = [txn("txn_re", "refund", "-40", source=refund_source)]
    sync_balance_transactions()
    record = StripeBalanceTransaction.objects.get()
    assert record.state == "needs_review" and "unapplied" in record.review_reason
    assert (record.refund, record.payment, PaymentRefund.objects.count()) == (None, None, 0)


@pytest.mark.parametrize("data, reason", [
    (txn("txn_dp", "dispute", "-100", fee="15", source="dp_1"), "'dispute' is not posted automatically"),
    (txn("txn_usd", "payout", "-10", currency="usd"), "USD is not the ledger currency"),
    (txn("txn_ch", "charge", "10", source="ch_1"), "has no payment intent"),
])
def test_unhandled_transactions_need_review_and_explain_the_difference(stripe_api, data, reason):
    stripe_api.listed = [data]
    sync_balance_transactions()
    record = StripeBalanceTransaction.objects.get()
    assert record.state == "needs_review" and reason in record.review_reason
    assert record.journal_entry is None
    if record.currency == "EUR":
        stripe_balance(stripe_api, pending=str(record.net))
        report = reconciliation_report()
        assert report["difference"] == record.net
        assert report["needs_review"] == {"count": 1, "net": record.net}
        assert report["unexplained"] == 0 and report["reconciled"] is False


def test_report_flags_payments_whose_charge_is_not_synced(posted, stripe_api):
    stripe_payment(posted)
    stripe_balance(stripe_api, pending="96.80")
    report = reconciliation_report()
    assert report["ledger_balance"] == Decimal("100") and report["stripe_balance"] == Decimal("96.80")
    assert report["unsynced_payments"] == {"count": 1, "amount": Decimal("100")}
    assert report["reconciled"] is False


def test_refund_service_only_credits_cash_accounts(posted):
    payment = stripe_payment(posted, allocate=None)
    with pytest.raises(ValidationError):
        refund_payment(payment, amount="10", reference="RF-1", refunded_on=date(2026, 1, 21), cash_account="5000")
    refund = refund_payment(payment, amount="10", reference="RF-2", refunded_on=date(2026, 1, 21),
                            cash_account="1010")
    assert refund.journal_entry.lines.filter(account__code="1010", credit=10).exists()


def test_api_permissions_sync_and_report(client, user, posted, stripe_api):
    stripe_payment(posted)
    stripe_api.listed = [charge()]
    assert client.post(BASE + "balance-transactions/sync/").status_code == 403
    grant(user, "add_stripebalancetransaction", "add_journalentry")
    user = type(user).objects.get(pk=user.pk)
    client.force_authenticate(user)
    response = client.post(BASE + "balance-transactions/sync/")
    assert (response.status_code, response.data) == (200, {"listed": 1, "posted": 1})

    listed = client.get(BASE + "balance-transactions/", {"state": "posted"})
    assert listed.status_code == 200 and listed.data["results"][0]["net"] == "96.80"
    stripe_balance(stripe_api, pending="96.80")
    report = client.get(BASE + "reconciliation/")
    assert report.status_code == 200
    assert (report.data["ledger_balance"], report.data["difference"], report.data["reconciled"]) == (
        "96.80", "0.00", True)


def test_api_reports_stripe_errors(client, user, settings, stripe_api):
    stripe_api.v1.balance.retrieve.side_effect = stripe.APIConnectionError("down")
    assert client.get(BASE + "reconciliation/").status_code == 502


def test_api_without_stripe_key_is_unavailable(client, settings):
    settings.STRIPE_SECRET_KEY = ""
    assert client.get(BASE + "reconciliation/").status_code == 503
