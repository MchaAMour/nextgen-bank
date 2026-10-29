"""Stripe balance transactions: post fees, payouts and refunds, and reconcile Stripe clearing (1010).

Checkout payments are already in the ledger at their gross amount (webhook). Every
movement of the Stripe balance is then synced once by its Stripe ID and posted so
that Stripe clearing follows the Stripe balance:

| Category | Entry |
| --- | --- |
| charge | Match the `stripe:<payment_intent>` payment; fee: debit 5100, credit 1010 |
| refund | Refund of the payment's unapplied credit: debit 1100, credit 1010 |
| payout, payout_reversal | Debit or credit cash and bank (1000) against 1010; payout fees to 5100 |
| fee | Stripe fees billed separately: debit 5100, credit 1010 |

Anything else, or anything that does not match the ledger, posts nothing and is
stored as needing review; reviews are retried on every sync.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .models import BASE_CURRENCY, CustomerPayment, JournalEntry, JournalLine, StripeBalanceTransaction
from .services import audit, post_lines, refund_payment
from .stripe_payments import MINOR_UNITS, _from_timestamp, stripe_client

CLEARING, CASH, FEES = "1010", "1000", "5100"
CLEARING_CATEGORIES = {"payout", "payout_reversal", "fee"}
# Transactions created up to this long before the newest stored one are listed again,
# which also refreshes their pending/available status.
SYNC_OVERLAP = timedelta(days=1)


class NeedsReview(Exception):
    """The transaction cannot be posted automatically; the message is stored as the reason."""


def _signed(code, amount):
    """A journal line from a debit-positive amount."""
    return (code, amount, Decimal("0")) if amount > 0 else (code, Decimal("0"), -amount)


def _amount(value):
    return Decimal(value) / MINOR_UNITS


def _cents(value):
    """Aggregates come back with extra precision on SQLite."""
    return (value or Decimal("0")).quantize(Decimal("0.01"))


def _as_dict(obj):
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def store_balance_transaction(data):
    """Store a Stripe balance transaction once; later syncs only refresh its Stripe status."""
    source = data.get("source")
    record, created = StripeBalanceTransaction.objects.get_or_create(stripe_id=data["id"], defaults={
        "reporting_category": data["reporting_category"],
        "amount": _amount(data["amount"]),
        "fee": _amount(data["fee"]),
        "net": _amount(data["net"]),
        "currency": data["currency"].upper(),
        "stripe_status": data["status"],
        "stripe_created": _from_timestamp(data["created"]),
        "source_id": (source.get("id") if isinstance(source, dict) else source) or "",
        "payload": data,
    })
    if not created and record.stripe_status != data["status"]:
        record.stripe_status = data["status"]
        record.save(update_fields=["stripe_status", "updated_at"])
    return record


def apply_balance_transaction(record, *, actor=None):
    """Post a stored transaction if it is not finished; returns it with its new state."""
    with transaction.atomic():
        record = StripeBalanceTransaction.objects.select_for_update().get(pk=record.pk)
        if record.state in StripeBalanceTransaction.FINISHED:
            return record
        try:
            with transaction.atomic():
                record.state = _post(record, actor)
                record.review_reason = ""
        except (NeedsReview, ValidationError, IntegrityError) as error:
            # The savepoint rolled back any partial posting.
            record.state = StripeBalanceTransaction.State.NEEDS_REVIEW
            record.review_reason = "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)
            record.payment = record.refund = record.journal_entry = None
        record.save()
        if record.state in StripeBalanceTransaction.FINISHED:
            audit(f"stripe_balance_transaction.{record.state}", record,
                  f"Stripe {record.reporting_category} {record.stripe_id}", actor=actor, net=str(record.net))
        return record


def _post(record, actor):
    if record.currency != BASE_CURRENCY:
        raise NeedsReview(f"Currency {record.currency} is not the ledger currency {BASE_CURRENCY}.")
    entry_date = timezone.localdate(record.stripe_created)
    category = record.reporting_category
    if category == "charge":
        record.payment = _stripe_payment(record)
        if record.payment.amount != record.amount:
            raise NeedsReview(f"Stripe charged {record.amount}; the payment records {record.payment.amount}.")
        lines = [_signed(FEES, record.fee), _signed(CLEARING, -record.fee)]
    elif category == "refund":
        if record.fee:
            raise NeedsReview("Refunds with a Stripe fee are not posted automatically.")
        record.payment = _stripe_payment(record)
        record.refund = refund_payment(record.payment, amount=-record.amount, reference=f"stripe:{record.source_id}",
                                       refunded_on=entry_date, actor=actor, cash_account=CLEARING)
        return StripeBalanceTransaction.State.POSTED
    elif category in CLEARING_CATEGORIES:
        # The non-clearing side is cash and bank for payouts and fees for Stripe fees.
        main = FEES if category == "fee" else CASH
        lines = [_signed(main, -record.amount), _signed(FEES, record.fee), _signed(CLEARING, record.net)]
    else:
        raise NeedsReview(f"Stripe category '{category}' is not posted automatically.")
    if not any(debit or credit for _, debit, credit in lines):
        return StripeBalanceTransaction.State.NOTHING_TO_POST
    record.journal_entry = post_lines(
        lines, currency=record.currency, source_type="stripe_balance_transaction", source_id=record.pk,
        entry_date=entry_date, memo=f"Stripe {category} {record.stripe_id}", actor=actor)
    return StripeBalanceTransaction.State.POSTED


def _stripe_payment(record):
    """The Checkout payment this charge or refund belongs to, recorded in Stripe clearing."""
    source = record.payload.get("source")
    payment_intent = source.get("payment_intent") if isinstance(source, dict) else None
    if not payment_intent:
        raise NeedsReview("The Stripe source has no payment intent.")
    payment = CustomerPayment.objects.filter(reference=f"stripe:{payment_intent}").first()
    if payment is None:
        raise NeedsReview(f"No payment is recorded for {payment_intent}; its webhook may not be processed yet.")
    if not payment.journal_entry.lines.filter(account__code=CLEARING, debit=payment.amount).exists():
        raise NeedsReview(f"Payment {payment.reference} was not recorded in Stripe clearing.")
    return payment


def sync_balance_transactions(*, client=None, actor=None):
    """List new Stripe balance transactions, store them, and post everything not yet finished."""
    client = client or stripe_client()
    params = {"limit": 100, "expand": ["data.source"]}
    latest = StripeBalanceTransaction.objects.order_by("-stripe_created").values_list(
        "stripe_created", flat=True).first()
    if latest:
        params["created"] = {"gte": int((latest - SYNC_OVERLAP).timestamp())}
    listed = 0
    for data in client.v1.balance_transactions.list(params).auto_paging_iter():
        store_balance_transaction(_as_dict(data))
        listed += 1
    counts = {"listed": listed}
    pending = StripeBalanceTransaction.objects.filter(state=StripeBalanceTransaction.State.NEEDS_REVIEW)
    for record in pending.order_by("stripe_created", "stripe_id"):
        state = apply_balance_transaction(record, actor=actor).state
        counts[state] = counts.get(state, 0) + 1
    return counts


def clearing_balance():
    totals = JournalLine.objects.filter(account__code=CLEARING, entry__status__in=JournalEntry.IN_LEDGER).aggregate(
        debit=Sum("debit"), credit=Sum("credit"))
    return _cents(totals["debit"]) - _cents(totals["credit"])


def reconciliation_report(*, client=None):
    """Compare Stripe clearing with the Stripe balance (available plus pending) in the ledger currency.

    Items needing review are in the Stripe balance but not in the ledger, so their net
    explains that part of the difference. Run a sync first: payments whose charge is
    not synced yet are in the ledger at gross while Stripe holds them net of fees.
    """
    client = client or stripe_client()
    balance = _as_dict(client.v1.balance.retrieve())
    stripe_total = _cents(sum((_amount(funds["amount"]) for key in ("available", "pending") for funds in balance[key]
                        if funds["currency"].upper() == BASE_CURRENCY), Decimal("0")))
    ledger = clearing_balance()
    review = StripeBalanceTransaction.objects.filter(state=StripeBalanceTransaction.State.NEEDS_REVIEW)
    review_net = _cents(review.aggregate(total=Sum("net"))["total"])
    unsynced = CustomerPayment.objects.filter(
        reference__startswith="stripe:", journal_entry__lines__account__code=CLEARING,
        journal_entry__lines__debit__gt=0).exclude(stripe_balance_transactions__reporting_category="charge")
    difference = stripe_total - ledger
    return {
        "currency": BASE_CURRENCY,
        "ledger_balance": ledger,
        "stripe_balance": stripe_total,
        "difference": difference,
        "needs_review": {"count": review.count(), "net": review_net},
        "unsynced_payments": {"count": unsynced.count(),
                              "amount": _cents(unsynced.aggregate(total=Sum("amount"))["total"])},
        "unexplained": difference - review_net,
        "reconciled": difference == 0 and not review.exists() and not unsynced.exists(),
    }
