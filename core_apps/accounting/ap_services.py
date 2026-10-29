"""Accounts payable: supplier bills with approval, supplier payments, allocations, aging and statements.

| Service | Accounting effect |
| --- | --- |
| create / replace / delete draft bill | None |
| approve_bill | None; a second person approves the draft |
| post_bill | Debit each line's account, credit AP (2000) |
| cancel_bill | Draft or approved: none. Posted without allocations: reverses the posting |
| record_supplier_payment | Debit AP, credit cash and bank (1000); optional allocations |
| allocate_supplier_payment | None; applies a payment to a bill |
"""

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (BASE_CURRENCY, Bill, BillLine, ChartOfAccount, Supplier, SupplierPayment,
    SupplierPaymentAllocation)
from .services import AGING_KEYS, _money, _replay, audit, post_lines, reverse_journal_entry

AP_ACCOUNT = "2000"
ZERO = Decimal("0.00")
# Lines may expense a purchase or capitalize it; control accounts have their own workflows.
LINE_ACCOUNT_TYPES = {ChartOfAccount.AccountType.EXPENSE, ChartOfAccount.AccountType.ASSET}
CONTROL_ACCOUNTS = {"1000", "1010", "1100", "1150", "2000"}


def _active_supplier(supplier):
    supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    if not supplier.is_active:
        raise ValidationError("Cannot record bills for an inactive supplier.")
    return supplier


def _add_lines(bill, lines):
    lines = list(lines)
    if not lines:
        raise ValidationError("A bill requires at least one line.")
    codes = {line["account"] for line in lines}
    accounts = {a.code: a for a in ChartOfAccount.objects.filter(code__in=codes, is_active=True,
                                                                 account_type__in=LINE_ACCOUNT_TYPES)}
    for line in lines:
        code = line["account"]
        if code in CONTROL_ACCOUNTS or code not in accounts:
            raise ValidationError(f"Account {code} is not an active expense or asset account for bill lines.")
        for field in ("quantity", "unit_price"):
            if isinstance(line.get(field), (float, bool)):
                raise ValidationError("Use Decimal or decimal strings for bill amounts.")
        BillLine.objects.create(bill=bill, account=accounts[code], description=line["description"],
                                quantity=line["quantity"], unit_price=line["unit_price"])


@transaction.atomic
def create_draft_bill(*, supplier, number, bill_date, due_date, lines, currency=BASE_CURRENCY, actor=None):
    supplier = _active_supplier(supplier)
    bill = Bill.objects.create(supplier=supplier, number=number, bill_date=bill_date, due_date=due_date,
                               currency=currency, created_by=_user(actor))
    _add_lines(bill, lines)
    audit("bill.drafted", bill, f"Draft bill {number} from {supplier.name}", actor=actor, total=str(bill.total))
    return bill


@transaction.atomic
def replace_draft_bill(bill, *, supplier, number, bill_date, due_date, lines, currency=BASE_CURRENCY, actor=None):
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.DRAFT:
        raise ValidationError("Only draft bills can be edited.")
    supplier = _active_supplier(supplier)
    for line in bill.lines.all():
        line.delete()
    bill.supplier, bill.number, bill.currency = supplier, number, currency
    bill.bill_date, bill.due_date = bill_date, due_date
    bill.save()
    _add_lines(bill, lines)
    audit("bill.replaced", bill, f"Draft bill {number} replaced", actor=actor, total=str(bill.total))
    return bill


@transaction.atomic
def delete_draft_bill(bill, *, actor=None):
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.DRAFT:
        raise ValidationError("Only draft bills can be deleted.")
    audit("bill.deleted", bill, f"Draft bill {bill.number} deleted", actor=actor)
    bill.delete()


def _user(actor):
    return actor if actor is not None and getattr(actor, "is_authenticated", False) else None


@transaction.atomic
def approve_bill(bill, *, actor):
    """Approve a complete draft. The approver must be a user other than the bill's creator."""
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    approver = _user(actor)
    if bill.status != Bill.Status.DRAFT:
        raise ValidationError("Only draft bills can be approved.")
    if approver is None:
        raise ValidationError("Bills are approved by a signed-in user.")
    if bill.created_by_id == approver.pk:
        raise ValidationError("A bill must be approved by someone other than its creator.")
    if not bill.supplier.is_active or bill.total <= 0:
        raise ValidationError("Approval requires an active supplier and a positive bill total.")
    bill._apply_transition(status=Bill.Status.APPROVED, approved_by=approver, approved_at=timezone.now())
    audit("bill.approved", bill, f"Bill {bill.number} approved", actor=approver, total=str(bill.total))
    return bill


@transaction.atomic
def post_bill(bill, *, actor=None):
    """Debit each line's account and credit AP for the approved total."""
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.APPROVED:
        raise ValidationError("Only approved bills can be posted.")
    by_account = defaultdict(lambda: ZERO)
    for line in bill.lines.select_related("account"):
        by_account[line.account.code] += line.total
    total = sum(by_account.values(), ZERO)
    lines = [(code, amount, ZERO) for code, amount in sorted(by_account.items())] + [(AP_ACCOUNT, ZERO, total)]
    entry = post_lines(lines, currency=bill.currency, source_type="bill", source_id=bill.pk,
                       entry_date=bill.bill_date, memo=f"Bill {bill.number} from {bill.supplier.name}", actor=actor)
    bill._apply_transition(status=Bill.Status.POSTED, posted_total=total, journal_entry=entry)
    audit("bill.posted", bill, f"Bill {bill.number} posted", actor=actor, total=str(total))
    return bill


@transaction.atomic
def cancel_bill(bill, *, cancellation_date, actor=None):
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status == Bill.Status.CANCELLED:
        raise ValidationError("Bill is already cancelled.")
    if cancellation_date < bill.bill_date:
        raise ValidationError("Cancellation cannot precede the bill date.")
    if bill.allocations.exists():
        raise ValidationError("Bills with allocated payments cannot be cancelled.")
    entry = None
    if bill.status == Bill.Status.POSTED:
        entry = reverse_journal_entry(bill.journal_entry, entry_date=cancellation_date,
                                      memo=f"Cancel bill {bill.number}", actor=actor)
    bill._apply_transition(status=Bill.Status.CANCELLED, cancellation_entry=entry)
    audit("bill.cancelled", bill, f"Bill {bill.number} cancelled", actor=actor,
          cancellation_date=cancellation_date.isoformat())
    return bill


@transaction.atomic
def record_supplier_payment(supplier, *, amount, reference, paid_on, allocations=(), currency=BASE_CURRENCY,
                            actor=None):
    """Post money paid (debit AP, credit cash and bank) and apply it to bills atomically.

    Identical retries, including every requested allocation, return the original payment.
    """
    amount = _money(amount, "Payment")
    allocations = list(allocations)
    existing = _replay(SupplierPayment, {"reference": reference}, {
        "supplier_id": supplier.pk, "amount": amount, "currency": currency, "paid_on": paid_on},
        "Supplier payment reference")
    if existing:
        for allocation in allocations:
            match = _replay(SupplierPaymentAllocation, {"reference": allocation["reference"]}, {
                "payment_id": existing.pk, "bill_id": allocation["bill"].pk,
                "amount": _money(allocation["amount"], "Allocation")}, "Allocation reference")
            if match is None:
                raise ValidationError("Supplier payment reference has already been used with different allocations.")
        return existing
    supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    payment = SupplierPayment(supplier=supplier, reference=reference, amount=amount, currency=currency,
                              paid_on=paid_on)
    payment.journal_entry = post_lines([(AP_ACCOUNT, amount, ZERO), ("1000", ZERO, amount)], currency=currency,
                                       source_type="supplier_payment", source_id=payment.pk, entry_date=paid_on,
                                       memo=f"Supplier payment {reference}", actor=actor)
    payment.save()
    audit("supplier_payment.recorded", payment, f"Payment {reference} to {supplier.name}", actor=actor,
          amount=str(amount))
    for allocation in allocations:
        allocate_supplier_payment(payment, bill=allocation["bill"], amount=allocation["amount"],
                                  reference=allocation["reference"], allocated_on=paid_on, actor=actor)
    return payment


@transaction.atomic
def allocate_supplier_payment(payment, *, bill, amount, reference, allocated_on, actor=None):
    """Apply unapplied payment to a posted bill. Locks payment, then bill."""
    amount = _money(amount, "Allocation")
    payment = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    existing = _replay(SupplierPaymentAllocation, {"reference": reference}, {
        "payment_id": payment.pk, "bill_id": bill.pk, "amount": amount, "allocated_on": allocated_on},
        "Allocation reference")
    if existing:
        return existing
    if bill.status != Bill.Status.POSTED:
        raise ValidationError("Payments can only be allocated to posted bills.")
    if bill.supplier_id != payment.supplier_id or bill.currency != payment.currency:
        raise ValidationError("Payment and bill must have the same supplier and currency.")
    if allocated_on < max(payment.paid_on, bill.bill_date):
        raise ValidationError("Allocation cannot precede the payment or bill date.")
    if amount > bill.open_balance:
        raise ValidationError("Allocation exceeds the open bill balance.")
    if amount > payment.unapplied_amount:
        raise ValidationError("Allocation exceeds the unapplied payment amount.")
    allocation = SupplierPaymentAllocation.objects.create(payment=payment, bill=bill, reference=reference,
                                                          amount=amount, allocated_on=allocated_on)
    audit("supplier_payment.allocated", allocation, f"Payment {payment.reference} allocated to bill {bill.number}",
          actor=actor, amount=str(amount), payment=str(payment.pk), bill=str(bill.pk))
    return allocation


def bill_aging(*, as_of):
    """Historical AP: open bills by days past due, less unapplied supplier prepayments.

    `net` = total - unapplied equals the AP (2000) ledger credit balance at the same date.
    """
    totals = {key: ZERO for key in AGING_KEYS}

    def dated_sum(records, date_field):
        return sum((r.amount for r in records if getattr(r, date_field) <= as_of), ZERO)

    bills = Bill.objects.filter(bill_date__lte=as_of, journal_entry__isnull=False).select_related(
        "cancellation_entry").prefetch_related("allocations")
    for bill in bills:
        if bill.cancellation_entry and bill.cancellation_entry.entry_date <= as_of:
            continue
        balance = bill.posted_total - dated_sum(bill.allocations.all(), "allocated_on")
        if balance == 0:
            continue
        days = (as_of - bill.due_date).days
        bucket = ("current" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60
                  else "61_90" if days <= 90 else "over_90")
        totals[bucket] += balance
        totals["total"] += balance
    for payment in SupplierPayment.objects.filter(paid_on__lte=as_of).prefetch_related("allocations"):
        totals["unapplied"] += payment.amount - dated_sum(payment.allocations.all(), "allocated_on")
    totals["net"] = totals["total"] - totals["unapplied"]
    return {BASE_CURRENCY: totals}


def supplier_statement(supplier, *, start_date, end_date):
    """What is owed to the supplier: opening balance, dated movements with a running balance, closing.

    Bills increase it; payments and cancellations reduce it. Closing balances of all
    suppliers add up to the AP (2000) ledger credit balance.
    """
    if start_date > end_date:
        raise ValidationError("The start date must not be after the end date.")
    movements = []
    for bill in Bill.objects.filter(supplier=supplier, journal_entry__isnull=False).select_related(
            "cancellation_entry"):
        movements.append((bill.bill_date, 0, "bill", bill.number, bill.posted_total, bill.pk))
        if bill.cancellation_entry_id:
            movements.append((bill.cancellation_entry.entry_date, 2, "cancellation", bill.number,
                              -bill.posted_total, bill.pk))
    for payment in SupplierPayment.objects.filter(supplier=supplier):
        movements.append((payment.paid_on, 1, "payment", payment.reference, -payment.amount, payment.pk))
    movements.sort(key=lambda m: m[:4])
    opening = sum((m[4] for m in movements if m[0] < start_date), ZERO)
    balance, lines = opening, []
    for day, _, kind, reference, amount, record_id in movements:
        if start_date <= day <= end_date:
            balance += amount
            lines.append({"date": day, "type": kind, "reference": reference, "id": record_id, "amount": amount,
                          "balance": balance})
    return {"supplier": {"id": supplier.pk, "name": supplier.name, "email": supplier.email},
            "start_date": start_date, "end_date": end_date, "currency": BASE_CURRENCY,
            "opening_balance": opening, "lines": lines, "closing_balance": balance}
