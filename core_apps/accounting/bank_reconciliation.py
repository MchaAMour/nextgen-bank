"""Bank statements for cash and bank (1000): import, matching to the ledger, and reconciliation.

Statement lines are matched one-to-one with posted ledger lines on the bank account
that move it by the same amount: customer payments, refunds, Stripe payouts and
anything else already in the books. Bank-only items, such as bank charges or
interest, are posted from the statement line and matched in one step.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from .models import (BASE_CURRENCY, BankMatch, BankStatement, BankStatementLine, ChartOfAccount, JournalEntry,
    JournalLine)
from .services import audit, post_lines

BANK_ACCOUNTS = {"1000"}
# Posting a bank-only item to these would bypass their subledgers or the Stripe sync.
CONTROL_ACCOUNTS = {"1000", "1010", "1100", "1150", "2000"}
# A bank line is booked on or after its ledger entry: payouts take days to arrive.
MATCH_DAYS_BEFORE, MATCH_DAYS_AFTER = 10, 3
CSV_COLUMNS = {"date", "amount", "description", "reference"}
ZERO = Decimal("0.00")


def _decimal(value, label, *, nonzero=False):
    """Signed amount with at most two decimal places; floats are rejected."""
    if isinstance(value, (float, bool)):
        raise ValidationError(f"Use a decimal {label}.")
    try:
        value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"Invalid {label}.")
    if not value.is_finite() or abs(value) >= Decimal("1e16") or value != value.quantize(Decimal("0.01")):
        raise ValidationError(f"The {label} must have at most two decimal places.")
    if nonzero and value == 0:
        raise ValidationError(f"The {label} cannot be zero.")
    return value.quantize(Decimal("0.01"))


def _cents(value):
    return (value or ZERO).quantize(Decimal("0.01"))


def _bank_account(code):
    if code not in BANK_ACCOUNTS:
        raise ValidationError(f"Account {code} is not a bank account.")
    # The lock serializes imports, so statements of one account cannot overlap.
    account = ChartOfAccount.objects.select_for_update().filter(code=code, is_active=True).first()
    if account is None:
        raise ValidationError(f"Bank account {code} is missing or inactive.")
    return account


def parse_statement_csv(text):
    """Lines from CSV with the columns date (YYYY-MM-DD), amount, description and reference."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not CSV_COLUMNS <= {name.strip().lower() for name in reader.fieldnames}:
        raise ValidationError(f"The CSV needs the columns {', '.join(sorted(CSV_COLUMNS))}.")
    lines = []
    for number, row in enumerate(reader, start=2):
        row = {key.strip().lower(): (value or "").strip() for key, value in row.items() if key}
        try:
            booked_on = date.fromisoformat(row["date"])
        except ValueError:
            raise ValidationError(f"Row {number}: use a YYYY-MM-DD date.")
        lines.append({"booked_on": booked_on, "amount": row["amount"], "description": row["description"],
                      "bank_reference": row["reference"]})
    return lines


def _line_key(line):
    return (line["booked_on"], line["amount"], line.get("description", ""), line.get("bank_reference", ""))


@transaction.atomic
def import_bank_statement(*, reference, start_date, end_date, opening_balance, closing_balance, lines,
                          account_code="1000", actor=None):
    """Import a complete statement and match its lines automatically. An identical retry returns the original."""
    opening, closing = _decimal(opening_balance, "opening balance"), _decimal(closing_balance, "closing balance")
    lines = [{**line, "amount": _decimal(line["amount"], "line amount", nonzero=True),
              "description": line.get("description", ""), "bank_reference": line.get("bank_reference", "")}
             for line in lines]
    account = _bank_account(account_code)
    existing = BankStatement.objects.filter(reference=reference).first()
    if existing:
        same = ((existing.account_id, existing.start_date, existing.end_date, existing.opening_balance,
                 existing.closing_balance) == (account.pk, start_date, end_date, opening, closing)
                and [_line_key(vars(line)) for line in existing.lines.order_by("sequence")]
                == [_line_key(line) for line in lines])
        if not same:
            raise ValidationError("This statement reference has already been imported with different details.")
        return existing
    if end_date < start_date:
        raise ValidationError("The statement cannot end before it starts.")
    if not lines:
        raise ValidationError("A statement requires at least one line.")
    if any(not start_date <= line["booked_on"] <= end_date for line in lines):
        raise ValidationError("Every line must be booked within the statement period.")
    if opening + sum(line["amount"] for line in lines) != closing:
        raise ValidationError("Opening balance plus the lines does not equal the closing balance.")
    previous = BankStatement.objects.filter(account=account).order_by("-end_date").first()
    if previous and start_date <= previous.end_date:
        raise ValidationError(f"Statements must follow {previous.reference}, which ends on {previous.end_date}.")
    if previous and opening != previous.closing_balance:
        raise ValidationError(f"The opening balance must equal the closing balance of {previous.reference} "
                              f"({previous.closing_balance}).")
    statement = BankStatement.objects.create(
        account=account, reference=reference, start_date=start_date, end_date=end_date, opening_balance=opening,
        closing_balance=closing, currency=BASE_CURRENCY, imported_by=actor)
    for sequence, line in enumerate(lines, start=1):
        BankStatementLine.objects.create(statement=statement, sequence=sequence, **line)
    audit("bank_statement.imported", statement, f"Bank statement {reference} imported", actor=actor,
          lines=len(lines), opening_balance=str(opening), closing_balance=str(closing))
    auto_match(statement=statement, actor=actor)
    return statement


def _ledger_amount(journal_line):
    return journal_line.debit - journal_line.credit


def _candidates(line):
    amount = Q(debit=line.amount) if line.amount > 0 else Q(credit=-line.amount)
    return (JournalLine.objects.filter(amount, account_id=line.statement.account_id, entry__status="posted",
                                       bank_match__isnull=True,
                                       entry__entry_date__gte=line.booked_on - timedelta(days=MATCH_DAYS_BEFORE),
                                       entry__entry_date__lte=line.booked_on + timedelta(days=MATCH_DAYS_AFTER))
            .select_related("entry").order_by("entry__entry_date", "created_at"))


def _best_candidate(line):
    """The single plausible ledger line, or None when there is none or the choice is ambiguous."""
    candidates = list(_candidates(line))
    if len(candidates) > 1 and line.bank_reference:
        by_reference = [c for c in candidates if line.bank_reference.lower() in
                        f"{c.entry.memo} {c.entry.reference} {c.description}".lower()]
        candidates = by_reference or candidates
    if len(candidates) > 1:
        distance = {c.pk: abs((line.booked_on - c.entry.entry_date).days) for c in candidates}
        nearest = min(distance.values())
        candidates = [c for c in candidates if distance[c.pk] == nearest]
    return candidates[0] if len(candidates) == 1 else None


@transaction.atomic
def auto_match(*, statement=None, actor=None):
    """Match unmatched statement lines that have exactly one plausible ledger line."""
    lines = BankStatementLine.objects.filter(match__isnull=True).select_related("statement")
    if statement is not None:
        lines = lines.filter(statement=statement)
    counts = {"matched": 0, "unmatched": 0}
    for line in lines.order_by("booked_on", "statement__start_date", "sequence"):
        candidate = _best_candidate(line)
        if candidate is None:
            counts["unmatched"] += 1
            continue
        _match(line, candidate, BankMatch.Method.AUTOMATIC, actor)
        counts["matched"] += 1
    return counts


def _match(line, journal_line, method, actor):
    match = BankMatch.objects.create(statement_line=line, journal_line=journal_line, method=method, matched_by=actor)
    audit("bank_line.matched", line, f"Bank line {line.statement.reference}/{line.sequence} matched", actor=actor,
          method=method, journal_line=str(journal_line.pk), entry=str(journal_line.entry_id), amount=str(line.amount))
    return match


def _unmatched_line(line):
    line = BankStatementLine.objects.select_for_update().select_related("statement").get(pk=line.pk)
    if BankMatch.objects.filter(statement_line=line).exists():
        raise ValidationError("This statement line is already matched.")
    return line


@transaction.atomic
def match_line(line, *, journal_line, actor=None):
    """Manually match a statement line with a posted ledger line of the same bank account and amount."""
    line = _unmatched_line(line)
    journal_line = JournalLine.objects.select_for_update().select_related("entry").get(pk=journal_line.pk)
    if journal_line.account_id != line.statement.account_id or journal_line.entry.status != "posted":
        raise ValidationError("Match a posted ledger line of the statement's bank account.")
    if _ledger_amount(journal_line) != line.amount:
        raise ValidationError("The ledger line must move the bank account by the statement line's amount.")
    if BankMatch.objects.filter(journal_line=journal_line).exists():
        raise ValidationError("This ledger line is already matched.")
    return _match(line, journal_line, BankMatch.Method.MANUAL, actor)


@transaction.atomic
def unmatch_line(line, *, actor=None):
    match = BankMatch.objects.select_for_update().select_related("statement_line__statement").filter(
        statement_line=line).first()
    if match is None:
        raise ValidationError("This statement line is not matched.")
    line = match.statement_line
    audit("bank_line.unmatched", line, f"Bank line {line.statement.reference}/{line.sequence} unmatched",
          actor=actor, method=match.method, journal_line=str(match.journal_line_id))
    match.delete()


@transaction.atomic
def post_statement_line(line, *, account_code, memo, actor=None):
    """Record a bank-only item (charges, interest) against another account and match it."""
    line = _unmatched_line(line)
    if account_code in CONTROL_ACCOUNTS:
        raise ValidationError(f"Account {account_code} is posted through its own workflow, not from the bank.")
    bank = line.statement.account.code
    amount = abs(line.amount)
    lines = ([(bank, amount, ZERO), (account_code, ZERO, amount)] if line.amount > 0
             else [(account_code, amount, ZERO), (bank, ZERO, amount)])
    entry = post_lines(lines, currency=line.statement.currency, source_type="bank_statement_line",
                       source_id=line.pk, entry_date=line.booked_on, memo=memo or line.description, actor=actor)
    return _match(line, entry.lines.get(account__code=bank), BankMatch.Method.POSTED, actor)


def _ledger_sum(lines):
    totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return _cents(totals["debit"]) - _cents(totals["credit"])


def bank_reconciliation(*, as_of, account_code="1000"):
    """Statement balance against the ledger at `as_of`, explained by the items matched on neither side.

    Ledger lines dated before the first statement are taken to be in its opening balance;
    any difference there is reported as `opening_difference`. Lines matched to a
    counterpart dated after `as_of` still count as reconciling items at `as_of`.
    """
    statements = BankStatement.objects.filter(account__code=account_code, start_date__lte=as_of)
    first = statements.order_by("start_date").first()
    if first is None:
        raise ValidationError("No bank statement covers this date.")
    covered_to = min(as_of, statements.order_by("-end_date").first().end_date)
    bank_lines = BankStatementLine.objects.filter(statement__account__code=account_code, booked_on__lte=as_of)
    bank_balance = first.opening_balance + _cents(bank_lines.aggregate(total=Sum("amount"))["total"])
    ledger = JournalLine.objects.filter(account__code=account_code, entry__status__in=JournalEntry.IN_LEDGER)
    ledger_balance = _ledger_sum(ledger.filter(entry__entry_date__lte=as_of))
    opening_difference = first.opening_balance - _ledger_sum(ledger.filter(entry__entry_date__lt=first.start_date))

    bank_items = bank_lines.exclude(match__journal_line__entry__entry_date__lte=as_of).select_related("statement")
    ledger_items = (ledger.filter(entry__entry_date__gte=first.start_date, entry__entry_date__lte=as_of)
                    .exclude(bank_match__statement_line__booked_on__lte=as_of).select_related("entry"))
    bank_items = [{"id": line.pk, "statement": line.statement.reference, "booked_on": line.booked_on,
                   "amount": line.amount, "description": line.description, "bank_reference": line.bank_reference}
                  for line in bank_items.order_by("booked_on", "sequence")]
    ledger_items = [{"id": line.pk, "entry": line.entry_id, "entry_date": line.entry.entry_date,
                     "amount": _ledger_amount(line), "memo": line.entry.memo}
                    for line in ledger_items.order_by("entry__entry_date", "created_at")]
    bank_total = sum((item["amount"] for item in bank_items), ZERO)
    ledger_total = sum((item["amount"] for item in ledger_items), ZERO)
    difference = bank_balance - ledger_balance
    return {
        "account": account_code,
        "as_of": as_of,
        "statements_cover_to": covered_to,
        "bank_balance": bank_balance,
        "ledger_balance": ledger_balance,
        "difference": difference,
        "opening_difference": opening_difference,
        # On the statement only: post or match them.
        "unmatched_bank_lines": {"count": len(bank_items), "total": bank_total, "items": bank_items},
        # In the ledger only: timing differences until the bank books them, or errors.
        "unmatched_ledger_lines": {"count": len(ledger_items), "total": ledger_total, "items": ledger_items},
        "unexplained": difference - opening_difference - bank_total + ledger_total,
        "reconciled": difference == 0 and opening_difference == 0 and not bank_items and not ledger_items,
    }
