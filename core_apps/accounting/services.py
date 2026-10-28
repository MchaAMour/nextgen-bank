from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (BASE_CURRENCY, ChartOfAccount, CreditNote, Customer, CustomerPayment, Invoice, InvoiceLine, JournalEntry,
    JournalLine, PaymentAllocation, PaymentRefund)


def _active_customer(customer):
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    if not customer.is_active:
        raise ValidationError("Cannot invoice an inactive customer.")
    return customer


def _required_lines(lines):
    lines = list(lines)
    if not lines:
        raise ValidationError("An invoice requires at least one line.")
    return lines


def _add_invoice_lines(invoice, lines):
    for line in lines:
        for field in ("quantity", "unit_price"):
            if isinstance(line.get(field), (float, bool)):
                raise ValidationError("Use Decimal or decimal strings for invoice amounts.")
        InvoiceLine.objects.create(invoice=invoice, **line)


@transaction.atomic
def create_draft_invoice(*, customer, number, issue_date, due_date, lines, currency=BASE_CURRENCY):
    """Create a complete draft atomically, without recording revenue or AR yet."""
    customer = _active_customer(customer)
    lines = _required_lines(lines)
    invoice = Invoice.objects.create(customer=customer, number=number,
                                     issue_date=issue_date, due_date=due_date, currency=currency)
    _add_invoice_lines(invoice, lines)
    return invoice


@transaction.atomic
def replace_draft_invoice(invoice, *, customer, number, issue_date, due_date, lines, currency=BASE_CURRENCY):
    """Replace a draft's header and lines under the posting lock, without ledger effect."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != "draft":
        raise ValidationError("Only draft invoices can be edited.")
    customer = _active_customer(customer)
    lines = _required_lines(lines)
    for line in invoice.lines.all():
        line.delete()
    invoice.customer, invoice.number, invoice.currency = customer, number, currency
    invoice.issue_date, invoice.due_date = issue_date, due_date
    invoice.save()
    _add_invoice_lines(invoice, lines)
    return invoice


@transaction.atomic
def delete_draft_invoice(invoice):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != "draft":
        raise ValidationError("Only draft invoices can be deleted.")
    invoice.delete()

User = get_user_model()


def post_journal_entry(entry: JournalEntry, *, posted_by: User | None = None) -> JournalEntry:
    """Validate and atomically post a balanced draft journal entry."""
    with transaction.atomic():
        locked_entry = (
            JournalEntry.objects.select_for_update()
            .prefetch_related("lines")
            .get(pk=entry.pk)
        )
        if locked_entry.status != JournalEntry.Status.DRAFT:
            raise ValidationError("Only draft journal entries can be posted.")

        lines = list(locked_entry.lines.all())
        if not lines:
            raise ValidationError("A journal entry requires at least one line.")

        currencies = {line.currency for line in lines}
        if len(currencies) != 1:
            raise ValidationError("A journal entry must use one currency.")

        total_debits = sum((line.debit for line in lines), Decimal("0.00"))
        total_credits = sum((line.credit for line in lines), Decimal("0.00"))
        if total_debits != total_credits:
            raise ValidationError("Journal entry debits and credits must balance.")

        locked_entry.status = JournalEntry.Status.POSTED
        locked_entry.posted_at = timezone.now()
        if posted_by is not None:
            locked_entry.posted_by = posted_by
        locked_entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])
        return locked_entry


def _post_pair(*, debit_code, credit_code, amount, currency, source_type, source_id, entry_date, memo,
               actor=None, reversal_of=None):
    accounts = {a.code: a for a in ChartOfAccount.objects.filter(code__in=[debit_code, credit_code])}
    if len(accounts) != 2 or not all(a.is_active for a in accounts.values()):
        raise ValidationError("Required posting accounts are missing or inactive.")
    entry = JournalEntry.objects.create(entry_date=entry_date, memo=memo,
        source_type=source_type, source_id=source_id, reversal_of=reversal_of)
    JournalLine.objects.create(entry=entry, account=accounts[debit_code], debit=amount, currency=currency)
    JournalLine.objects.create(entry=entry, account=accounts[credit_code], credit=amount, currency=currency)
    return post_journal_entry(entry, posted_by=actor)


def _post_for_invoice(invoice, **kwargs):
    return _post_pair(currency=invoice.currency, source_type="invoice", source_id=invoice.pk, **kwargs)


def _money(amount, label):
    """Positive amount with at most two decimal places; floats are rejected."""
    if isinstance(amount, (float, bool)):
        raise ValidationError(f"Use a decimal {label.lower()} amount.")
    try:
        amount = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"Invalid {label.lower()} amount.")
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e16") or amount != amount.quantize(Decimal("0.01")):
        raise ValidationError(f"{label} amount must be positive with at most two decimal places.")
    return amount


def _replay(model, lookup, expected, label):
    """Return the record a retried request already created, or None; reject reuse with other details."""
    existing = model.objects.filter(**lookup).first()
    if existing and any(getattr(existing, field) != value for field, value in expected.items()):
        raise ValidationError(f"{label} has already been used with different details.")
    return existing


@transaction.atomic
def post_invoice(invoice, *, actor=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != "draft":
        raise ValidationError("Only draft invoices can be posted.")
    invoice.full_clean()
    if not invoice.customer.is_active or not invoice.lines.exists() or invoice.total <= 0:
        raise ValidationError("Posting requires an active customer and a positive invoice total.")
    total = invoice.total
    entry = _post_for_invoice(invoice, debit_code="1100", credit_code="4000", amount=total,
        entry_date=invoice.issue_date, memo=f"Invoice {invoice.number}", actor=actor)
    invoice.status = "posted"
    invoice.posted_total = total
    invoice.journal_entry = entry
    invoice.save()
    return invoice


@transaction.atomic
def cancel_invoice(invoice, *, cancellation_date, actor=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == "cancelled":
        raise ValidationError("Invoice is already cancelled.")
    if cancellation_date < invoice.issue_date:
        raise ValidationError("Cancellation cannot precede the invoice date.")
    if invoice.allocations.exists() or invoice.credit_notes.exists():
        raise ValidationError("Invoices with allocated payments or credit notes cannot be cancelled; "
                              "issue a credit note for the open balance instead.")
    entry = None
    if invoice.status == "posted":
        entry = _post_for_invoice(invoice, debit_code="4000", credit_code="1100", amount=invoice.posted_total,
            entry_date=cancellation_date, memo=f"Cancel {invoice.number}",
            actor=actor, reversal_of=invoice.journal_entry)
    # Deliberate lifecycle transition after validation under the parent row lock.
    invoice._apply_transition(status="cancelled", cancellation_entry=entry)
    return invoice


CASH_ACCOUNTS = {"1000", "1010"}  # Cash and bank; Stripe clearing until payout reconciliation.


@transaction.atomic
def record_customer_payment(customer, *, amount, currency, reference, received_on, allocations=(), actor=None,
                            cash_account="1000"):
    """Post money received (debit cash_account, credit AR) and apply it to invoices atomically.

    Each allocation is a dict with invoice, amount and its own unique reference. A
    retry with identical details, including every requested allocation, returns the
    original payment. Any unallocated amount remains customer credit in AR.
    """
    amount = _money(amount, "Payment")
    if cash_account not in CASH_ACCOUNTS:
        raise ValidationError("Payments must debit cash and bank or Stripe clearing.")
    allocations = list(allocations)
    existing = _replay(CustomerPayment, {"reference": reference}, {"customer_id": customer.pk,
        "amount": amount, "currency": currency, "received_on": received_on}, "Payment reference")
    if existing:
        for allocation in allocations:
            match = _replay(PaymentAllocation, {"reference": allocation["reference"]}, {
                "payment_id": existing.pk, "invoice_id": allocation["invoice"].pk,
                "amount": _money(allocation["amount"], "Allocation")}, "Allocation reference")
            if match is None:
                raise ValidationError("Payment reference has already been used with different allocations.")
        return existing
    payment = CustomerPayment(customer=customer, reference=reference, amount=amount,
                              currency=currency, received_on=received_on)
    payment.journal_entry = _post_pair(debit_code=cash_account, credit_code="1100", amount=amount, currency=currency,
        source_type="customer_payment", source_id=payment.pk, entry_date=received_on,
        memo=f"Payment {reference}", actor=actor)
    payment.save()
    for allocation in allocations:
        allocate_payment(payment, invoice=allocation["invoice"], amount=allocation["amount"],
                         reference=allocation["reference"], allocated_on=received_on)
    return payment


@transaction.atomic
def allocate_payment(payment, *, invoice, amount, reference, allocated_on):
    """Apply unapplied payment credit to an open invoice. Locks payment, then invoice."""
    amount = _money(amount, "Allocation")
    payment = CustomerPayment.objects.select_for_update().get(pk=payment.pk)
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    existing = _replay(PaymentAllocation, {"reference": reference}, {"payment_id": payment.pk,
        "invoice_id": invoice.pk, "amount": amount, "allocated_on": allocated_on}, "Allocation reference")
    if existing:
        return existing
    if invoice.status != "posted":
        raise ValidationError("Payments can only be allocated to posted invoices.")
    if invoice.customer_id != payment.customer_id or invoice.currency != payment.currency:
        raise ValidationError("Payment and invoice must have the same customer and currency.")
    if allocated_on < max(payment.received_on, invoice.issue_date):
        raise ValidationError("Allocation cannot precede the payment or invoice date.")
    if amount > invoice.open_balance:
        raise ValidationError("Allocation exceeds the open invoice balance.")
    if amount > payment.unapplied_amount:
        raise ValidationError("Allocation exceeds the unapplied payment amount.")
    return PaymentAllocation.objects.create(payment=payment, invoice=invoice, reference=reference,
                                            amount=amount, allocated_on=allocated_on)


def receive_invoice_payment(invoice, *, amount, reference, received_on, actor=None):
    """Shortcut: one payment fully allocated to one invoice, sharing its reference."""
    invoice = Invoice.objects.get(pk=invoice.pk)
    return record_customer_payment(invoice.customer, amount=amount, currency=invoice.currency,
        reference=reference, received_on=received_on, actor=actor,
        allocations=[{"invoice": invoice, "amount": amount, "reference": reference}])


@transaction.atomic
def refund_payment(payment, *, amount, reference, refunded_on, actor=None):
    """Return unapplied customer credit: debit AR, credit cash."""
    amount = _money(amount, "Refund")
    payment = CustomerPayment.objects.select_for_update().get(pk=payment.pk)
    existing = _replay(PaymentRefund, {"reference": reference}, {"payment_id": payment.pk,
        "amount": amount, "refunded_on": refunded_on}, "Refund reference")
    if existing:
        return existing
    if refunded_on < payment.received_on:
        raise ValidationError("Refund cannot precede the payment date.")
    if amount > payment.unapplied_amount:
        raise ValidationError("Refund exceeds the unapplied payment amount.")
    refund = PaymentRefund(payment=payment, reference=reference, amount=amount, refunded_on=refunded_on)
    refund.journal_entry = _post_pair(debit_code="1100", credit_code="1000", amount=amount,
        currency=payment.currency, source_type="payment_refund", source_id=refund.pk,
        entry_date=refunded_on, memo=f"Refund {reference}", actor=actor)
    refund.save()
    return refund


@transaction.atomic
def issue_credit_note(invoice, *, number, amount, issued_on, reason, actor=None):
    """Reduce a posted invoice's open balance: debit sales, credit AR."""
    amount = _money(amount, "Credit note")
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    existing = _replay(CreditNote, {"number": number}, {"invoice_id": invoice.pk, "amount": amount,
        "issued_on": issued_on, "reason": reason}, "Credit note number")
    if existing:
        return existing
    if invoice.status != "posted":
        raise ValidationError("Credit notes require a posted invoice.")
    if issued_on < invoice.issue_date:
        raise ValidationError("Credit note cannot precede the invoice date.")
    if amount > invoice.open_balance:
        raise ValidationError("Credit note exceeds the open invoice balance.")
    note = CreditNote(invoice=invoice, number=number, amount=amount, issued_on=issued_on, reason=reason)
    note.journal_entry = _post_pair(debit_code="4000", credit_code="1100", amount=amount,
        currency=invoice.currency, source_type="credit_note", source_id=note.pk,
        entry_date=issued_on, memo=f"Credit note {number}", actor=actor)
    note.save()
    return note


AGING_KEYS = ["current", "1_30", "31_60", "61_90", "over_90", "total", "unapplied", "net"]


def invoice_aging(*, as_of):
    """Historical AR by currency: open invoices by days past due, less unapplied customer credit.

    `total` sums open invoices; `net` = total - unapplied, which equals the AR (1100)
    ledger balance at the same date.
    """
    groups = {}

    def group(currency):
        return groups.setdefault(currency, {key: Decimal("0.00") for key in AGING_KEYS})

    def dated_sum(records, date_field):
        return sum((r.amount for r in records if getattr(r, date_field) <= as_of), Decimal("0.00"))

    invoices = Invoice.objects.filter(issue_date__lte=as_of, journal_entry__isnull=False).select_related(
        "cancellation_entry").prefetch_related("allocations", "credit_notes")
    for invoice in invoices:
        if invoice.cancellation_entry and invoice.cancellation_entry.entry_date <= as_of:
            continue
        balance = (invoice.posted_total - dated_sum(invoice.allocations.all(), "allocated_on")
                   - dated_sum(invoice.credit_notes.all(), "issued_on"))
        if balance == 0:
            continue
        days = (as_of - invoice.due_date).days
        bucket = "current" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"
        totals = group(invoice.currency)
        totals[bucket] += balance
        totals["total"] += balance
    payments = CustomerPayment.objects.filter(received_on__lte=as_of).prefetch_related("allocations", "refunds")
    for payment in payments:
        unapplied = (payment.amount - dated_sum(payment.allocations.all(), "allocated_on")
                     - dated_sum(payment.refunds.all(), "refunded_on"))
        if unapplied:
            group(payment.currency)["unapplied"] += unapplied
    for totals in groups.values():
        totals["net"] = totals["total"] - totals["unapplied"]
    return groups
