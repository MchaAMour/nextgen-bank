from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Customer, ChartOfAccount, Invoice, InvoiceLine, InvoiceReceipt, JournalEntry, JournalLine


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
def create_draft_invoice(*, customer, number, issue_date, due_date, lines, currency="USD"):
    """Create a complete draft atomically, without recording revenue or AR yet."""
    customer = _active_customer(customer)
    lines = _required_lines(lines)
    invoice = Invoice.objects.create(customer=customer, number=number,
                                     issue_date=issue_date, due_date=due_date, currency=currency)
    _add_invoice_lines(invoice, lines)
    return invoice


@transaction.atomic
def replace_draft_invoice(invoice, *, customer, number, issue_date, due_date, lines, currency="USD"):
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


def _post_pair(*, debit_code, credit_code, amount, invoice, entry_date, memo, actor=None, reversal_of=None):
    accounts = {a.code: a for a in ChartOfAccount.objects.filter(code__in=[debit_code, credit_code])}
    if len(accounts) != 2 or not all(a.is_active for a in accounts.values()):
        raise ValidationError("Required posting accounts are missing or inactive.")
    entry = JournalEntry.objects.create(entry_date=entry_date, memo=memo,
        source_type="invoice", source_id=invoice.pk, reversal_of=reversal_of)
    JournalLine.objects.create(entry=entry, account=accounts[debit_code], debit=amount, currency=invoice.currency)
    JournalLine.objects.create(entry=entry, account=accounts[credit_code], credit=amount, currency=invoice.currency)
    return post_journal_entry(entry, posted_by=actor)


@transaction.atomic
def post_invoice(invoice, *, actor=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status != "draft":
        raise ValidationError("Only draft invoices can be posted.")
    invoice.full_clean()
    if not invoice.customer.is_active or not invoice.lines.exists() or invoice.total <= 0:
        raise ValidationError("Posting requires an active customer and a positive invoice total.")
    total = invoice.total
    entry = _post_pair(debit_code="1100", credit_code="4000", amount=total,
        invoice=invoice, entry_date=invoice.issue_date, memo=f"Invoice {invoice.number}", actor=actor)
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
    if invoice.receipts.exists():
        raise ValidationError("Refund allocated receipts before cancelling this invoice.")
    entry = None
    if invoice.status == "posted":
        entry = _post_pair(debit_code="4000", credit_code="1100", amount=invoice.posted_total,
            invoice=invoice, entry_date=cancellation_date, memo=f"Cancel {invoice.number}",
            actor=actor, reversal_of=invoice.journal_entry)
    # Deliberate lifecycle transition after validation under the parent row lock.
    invoice._apply_transition(status="cancelled", cancellation_entry=entry)
    return invoice


@transaction.atomic
def receive_invoice_payment(invoice, *, amount, reference, received_on, actor=None):
    from decimal import InvalidOperation
    if isinstance(amount, (float, bool)):
        raise ValidationError("Use a decimal amount.")
    try:
        amount = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Invalid receipt amount.")
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e16") or amount != amount.quantize(Decimal("0.01")):
        raise ValidationError("Receipt amount must be positive with at most two decimal places.")
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    existing = InvoiceReceipt.objects.filter(reference=reference).first()
    if existing:
        if existing.invoice_id == invoice.pk and existing.amount == amount and existing.received_on == received_on:
            return existing
        raise ValidationError("Receipt reference has already been used with different details.")
    if invoice.status != "posted" or received_on < invoice.issue_date:
        raise ValidationError("Receipt requires a posted invoice and a valid date.")
    if amount > invoice.open_balance:
        raise ValidationError("Receipt exceeds the open invoice balance.")
    entry = _post_pair(debit_code="1000", credit_code="1100", amount=amount,
        invoice=invoice, entry_date=received_on, memo=f"Receipt {reference}", actor=actor)
    return InvoiceReceipt.objects.create(invoice=invoice, reference=reference,
        amount=amount, received_on=received_on, journal_entry=entry)


def invoice_aging(*, as_of):
    """Historical open AR, grouped by currency and days past due."""
    groups = {}
    invoices = Invoice.objects.filter(issue_date__lte=as_of, journal_entry__isnull=False).select_related(
        "cancellation_entry").prefetch_related("receipts")
    for invoice in invoices:
        if invoice.cancellation_entry and invoice.cancellation_entry.entry_date <= as_of:
            continue
        balance = invoice.posted_total - sum((r.amount for r in invoice.receipts.all()
                                              if r.received_on <= as_of), Decimal("0"))
        if balance == 0:
            continue
        days = (as_of - invoice.due_date).days
        bucket = "current" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"
        group = groups.setdefault(invoice.currency, {k: Decimal("0.00") for k in
            ["current", "1_30", "31_60", "61_90", "over_90", "total"]})
        group[bucket] += balance
        group["total"] += balance
    return groups
