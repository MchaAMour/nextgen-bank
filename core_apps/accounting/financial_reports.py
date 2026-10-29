"""Balance sheet, profit and loss, and customer statements from the posted ledger and the AR subledger.

Year-end closes move profit to retained earnings (3000); the balance sheet shows profit
not yet transferred as current earnings, and profit and loss leaves closing entries out.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum

from .models import BASE_CURRENCY, ChartOfAccount, CreditNote, CustomerPayment, Invoice, JournalEntry, JournalLine, PaymentRefund

ZERO = Decimal("0.00")
BALANCE_SHEET_TYPES = ["asset", "liability", "equity"]
INCOME_TYPES = ["income", "expense"]


def _cents(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _account_balances(lines, account_types):
    """Balances in each account's normal direction, for accounts with a non-zero balance."""
    rows = (lines.filter(account__account_type__in=account_types).order_by()
            .values("account__code", "account__name", "account__account_type", "account__normal_balance")
            .annotate(debit=Sum("debit"), credit=Sum("credit")).order_by("account__code"))
    groups = {account_type: [] for account_type in account_types}
    for row in rows:
        net = _cents(row["debit"]) - _cents(row["credit"])
        balance = net if row["account__normal_balance"] == ChartOfAccount.NormalBalance.DEBIT else -net
        if balance:
            groups[row["account__account_type"]].append(
                {"code": row["account__code"], "name": row["account__name"], "balance": balance})
    return groups


def _section(accounts):
    return {"accounts": accounts, "total": sum((account["balance"] for account in accounts), ZERO)}


def _ledger_lines(**date_filters):
    return JournalLine.objects.filter(entry__status__in=JournalEntry.IN_LEDGER, **date_filters)


def balance_sheet(*, as_of):
    groups = _account_balances(_ledger_lines(entry__entry_date__lte=as_of), BALANCE_SHEET_TYPES + INCOME_TYPES)
    earnings = (sum((a["balance"] for a in groups["income"]), ZERO)
                - sum((a["balance"] for a in groups["expense"]), ZERO))
    assets, liabilities = _section(groups["asset"]), _section(groups["liability"])
    equity = _section(groups["equity"])
    equity["current_earnings"] = earnings
    equity["total"] += earnings
    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "liabilities_and_equity": liabilities["total"] + equity["total"],
        "balanced": assets["total"] == liabilities["total"] + equity["total"],
    }


def profit_and_loss(*, start_date, end_date):
    if start_date > end_date:
        raise ValidationError("The start date must not be after the end date.")
    # Year-end closing entries move profit to retained earnings; they are not income or expense.
    lines = _ledger_lines(entry__entry_date__gte=start_date, entry__entry_date__lte=end_date).exclude(
        entry__source_type="period_close")
    groups = _account_balances(lines, INCOME_TYPES)
    income, expenses = _section(groups["income"]), _section(groups["expense"])
    return {"start_date": start_date, "end_date": end_date, "income": income, "expenses": expenses,
            "net_income": income["total"] - expenses["total"]}


def _customer_movements(customer):
    """Every change to the customer's AR balance: (date, kind, reference, amount, id); positive = owed more."""
    invoices = Invoice.objects.filter(customer=customer, journal_entry__isnull=False).select_related(
        "cancellation_entry")
    for invoice in invoices:
        yield invoice.issue_date, "invoice", invoice.number, invoice.posted_total, invoice.pk
        if invoice.cancellation_entry_id:
            yield (invoice.cancellation_entry.entry_date, "cancellation", invoice.number, -invoice.posted_total,
                   invoice.pk)
    for payment in CustomerPayment.objects.filter(customer=customer):
        yield payment.received_on, "payment", payment.reference, -payment.amount, payment.pk
    for refund in PaymentRefund.objects.filter(payment__customer=customer):
        yield refund.refunded_on, "refund", refund.reference, refund.amount, refund.pk
    for note in CreditNote.objects.filter(invoice__customer=customer):
        yield note.issued_on, "credit_note", note.number, -note.amount, note.pk


def customer_statement(customer, *, start_date, end_date):
    """Opening balance, dated movements with a running balance, and closing balance (what the customer owes).

    A negative balance is credit in the customer's favour. Closing balances of all
    customers add up to the AR (1100) ledger balance.
    """
    if start_date > end_date:
        raise ValidationError("The start date must not be after the end date.")
    order = {"invoice": 0, "credit_note": 1, "payment": 2, "refund": 3, "cancellation": 4}
    movements = sorted(_customer_movements(customer), key=lambda m: (m[0], order[m[1]], m[2]))
    opening = sum((amount for day, _, _, amount, _ in movements if day < start_date), ZERO)
    balance, lines = opening, []
    for day, kind, reference, amount, record_id in movements:
        if start_date <= day <= end_date:
            balance += amount
            lines.append({"date": day, "type": kind, "reference": reference, "id": record_id,
                          "amount": amount, "balance": balance})
    return {"customer": {"id": customer.pk, "name": customer.name, "email": customer.email},
            "start_date": start_date, "end_date": end_date, "currency": BASE_CURRENCY, "opening_balance": opening,
            "lines": lines, "closing_balance": balance}
