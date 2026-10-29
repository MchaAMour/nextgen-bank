"""Period closing (AR-05): lock posted history, and optionally move the year's profit to retained earnings."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import BASE_CURRENCY, ChartOfAccount, JournalEntry, JournalLine, PeriodClose
from .services import audit, post_lines

RETAINED_EARNINGS = "3000"
CLOSING_SOURCE = "period_close"
ZERO = Decimal("0.00")


def _cents(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _closing_lines(through):
    """Lines that bring every income and expense balance up to `through` to zero, against retained earnings."""
    rows = (JournalLine.objects.filter(entry__status__in=JournalEntry.IN_LEDGER, entry__entry_date__lte=through,
                                       account__account_type__in=[ChartOfAccount.AccountType.INCOME,
                                                                  ChartOfAccount.AccountType.EXPENSE])
            .order_by().values("account__code").annotate(debit=Sum("debit"), credit=Sum("credit"))
            .order_by("account__code"))
    lines, profit = [], ZERO
    for row in rows:
        net = _cents(row["debit"]) - _cents(row["credit"])  # Debit-positive.
        if net:
            lines.append((row["account__code"], -net if net < 0 else ZERO, net if net > 0 else ZERO))
            profit -= net
    if profit:
        lines.append((RETAINED_EARNINGS, -profit if profit < 0 else ZERO, profit if profit > 0 else ZERO))
    return lines, profit


@transaction.atomic
def close_period(*, through, transfer_earnings=False, actor=None):
    """Close the books through a date. With transfer_earnings (a year end), first post a closing
    entry on that date moving income and expense balances to retained earnings (3000)."""
    # Lock the newest close so two closes cannot interleave.
    last = PeriodClose.objects.select_for_update().order_by("-closed_through").first()
    if last and through <= last.closed_through:
        raise ValidationError(f"The books are already closed through {last.closed_through}.")
    if JournalEntry.objects.filter(status=JournalEntry.Status.DRAFT, entry_date__lte=through).exists():
        raise ValidationError("Post or delete the draft entries dated in this period first.")
    entry, profit = None, ZERO
    if transfer_earnings:
        lines, profit = _closing_lines(through)
        if lines:
            entry = post_lines(lines, currency=BASE_CURRENCY, source_type=CLOSING_SOURCE, source_id=None,
                               entry_date=through, memo=f"Close income and expense through {through}", actor=actor)
    close = PeriodClose.objects.create(closed_through=through, closing_entry=entry,
                                       closed_by=actor if getattr(actor, "is_authenticated", False) else None)
    audit("period.closed", close, f"Books closed through {through}", actor=actor,
          transfer_earnings=transfer_earnings, profit_transferred=str(profit))
    return close

