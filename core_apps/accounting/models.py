from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core_apps.common.models import TimeStampedModel

User = get_user_model()
INVOICE_CURRENCIES = ("USD", "EUR", "GBP")


class GuardedQuerySet(models.QuerySet):
    """Bulk writes bypass instance validation; require validated instance writes."""

    label = "record"

    def update(self, **kwargs):
        raise ValidationError(f"Bulk {self.label} updates are prohibited.")

    def delete(self):
        raise ValidationError(f"Bulk {self.label} deletion is prohibited.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError(f"Bulk {self.label} updates are prohibited.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError(f"Bulk {self.label} creation is prohibited.")


class ARQuerySet(GuardedQuerySet):
    label = "AR document"


def _amount_sum(model, link, **filters):
    """Correlated SUM(amount) per outer row; subqueries avoid join row multiplication."""
    rows = (model.objects.filter(**{link: models.OuterRef("pk")}, **filters).order_by()
            .values(link).annotate(total=models.Sum("amount")).values("total"))
    return Coalesce(models.Subquery(rows), models.Value(Decimal("0.00")),
                    output_field=models.DecimalField(max_digits=18, decimal_places=2))


class InvoiceQuerySet(ARQuerySet):
    def with_balances(self):
        return self.annotate(allocated_total=_amount_sum(PaymentAllocation, "invoice"),
                             credited_total=_amount_sum(CreditNote, "invoice"))


class PaymentQuerySet(ARQuerySet):
    def with_balances(self):
        return self.annotate(allocated_total=_amount_sum(PaymentAllocation, "payment"),
                             refunded_total=_amount_sum(PaymentRefund, "payment"))


def _stored_total(obj, annotation, related):
    if hasattr(obj, annotation):
        return getattr(obj, annotation)
    return getattr(obj, related).aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")


class Customer(TimeStampedModel):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Invoice(TimeStampedModel):
    """Lifecycle changes go through services; database triggers reject other writes."""

    objects = InvoiceQuerySet.as_manager()

    number = models.CharField(max_length=40, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, default="draft", choices=[
        ("draft", "Draft"), ("posted", "Posted"), ("cancelled", "Cancelled")])
    posted_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    journal_entry = models.OneToOneField("JournalEntry", null=True, blank=True,
                                        on_delete=models.PROTECT, related_name="invoice")
    cancellation_entry = models.OneToOneField("JournalEntry", null=True, blank=True,
                                             on_delete=models.PROTECT, related_name="cancelled_invoice")

    class Meta:
        permissions = [("post_invoice", "Can post invoices"), ("cancel_invoice", "Can cancel invoices")]
        constraints = [
            models.CheckConstraint(check=models.Q(due_date__gte=models.F("issue_date")),
                                   name="invoice_due_not_before_issue"),
            models.CheckConstraint(check=models.Q(currency__in=INVOICE_CURRENCIES),
                                   name="invoice_currency_supported"),
            models.CheckConstraint(name="invoice_lifecycle_consistent", check=(
                models.Q(status="draft", journal_entry__isnull=True, posted_total__isnull=True,
                         cancellation_entry__isnull=True)
                | models.Q(status="posted", journal_entry__isnull=False, posted_total__gt=0,
                           cancellation_entry__isnull=True)
                | models.Q(status="cancelled", journal_entry__isnull=True, posted_total__isnull=True,
                           cancellation_entry__isnull=True)
                | models.Q(status="cancelled", journal_entry__isnull=False, posted_total__gt=0,
                           cancellation_entry__isnull=False))),
        ]

    def clean(self):
        super().clean()
        if self.issue_date and self.due_date and self.due_date < self.issue_date:
            raise ValidationError({"due_date": "Due date must not precede issue date."})
        if self.currency not in INVOICE_CURRENCIES:
            raise ValidationError({"currency": "Draft invoices currently support USD, EUR and GBP."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Lock the stored row so a stale draft object cannot overwrite a concurrent posting.
        original = Invoice.objects.select_for_update().filter(pk=self.pk).first()
        if original and original.status != "draft":
            raise ValidationError("Posted invoices must be changed through accounting services.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def _apply_transition(self, **fields):
        """Service-only lifecycle change; the caller holds the row lock and triggers validate it."""
        for name, value in fields.items():
            setattr(self, name, value)
        super().save(update_fields=[*fields, "updated_at"])

    @property
    def total(self):
        # Round each line first, then sum; this matches displayed document lines.
        return sum((line.total for line in self.lines.all()), Decimal("0.00"))

    @property
    def open_balance(self):
        """Current balance; use Invoice.objects.with_balances() to avoid per-row queries."""
        if self.status != "posted":
            return Decimal("0.00")
        return (self.posted_total - _stored_total(self, "allocated_total", "allocations")
                - _stored_total(self, "credited_total", "credit_notes"))

    def delete(self, *args, **kwargs):
        if Invoice.objects.filter(pk=self.pk).exclude(status="draft").exists():
            raise ValidationError("Posted invoices cannot be deleted.")
        return super().delete(*args, **kwargs)


def _lock_draft_invoices(*invoice_ids, message):
    """Share the posting service's parent row lock before changing draft lines."""
    statuses = Invoice.objects.select_for_update().filter(
        pk__in=[pk for pk in invoice_ids if pk]).values_list("status", flat=True)
    if any(status != "draft" for status in statuses):
        raise ValidationError(message)


class InvoiceLine(TimeStampedModel):
    objects = ARQuerySet.as_manager()

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=3,
                                   validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2,
                                     validators=[MinValueValidator(Decimal("0.00"))])

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="invoice_quantity_positive"),
            models.CheckConstraint(check=models.Q(unit_price__gte=0), name="invoice_price_nonnegative"),
        ]

    @transaction.atomic
    def save(self, *args, **kwargs):
        original_parent = InvoiceLine.objects.filter(pk=self.pk).values_list("invoice_id", flat=True).first()
        _lock_draft_invoices(original_parent, self.invoice_id, message="Posted invoice lines are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    @transaction.atomic
    def delete(self, *args, **kwargs):
        parent = InvoiceLine.objects.filter(pk=self.pk).values_list("invoice_id", flat=True).first()
        _lock_draft_invoices(parent, message="Posted invoice lines are immutable.")
        return super().delete(*args, **kwargs)

    @property
    def total(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class AppendOnlyRecord(TimeStampedModel):
    """Written once by services; corrections are new records. Triggers enforce this too."""

    objects = ARQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(f"{self._meta.verbose_name.capitalize()} records are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self._meta.verbose_name.capitalize()} records cannot be deleted.")


class CustomerPayment(AppendOnlyRecord):
    """Money received from a customer. Posting credits AR in full; allocations apply it to invoices."""

    objects = PaymentQuerySet.as_manager()

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payments")
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    received_on = models.DateField()
    journal_entry = models.OneToOneField("JournalEntry", on_delete=models.PROTECT, related_name="customer_payment")

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name="customer_payment_positive"),
            models.CheckConstraint(check=models.Q(currency__in=INVOICE_CURRENCIES),
                                   name="customer_payment_currency_supported"),
        ]

    @property
    def unapplied_amount(self):
        """Customer credit still held in AR: not allocated to invoices and not refunded."""
        return (self.amount - _stored_total(self, "allocated_total", "allocations")
                - _stored_total(self, "refunded_total", "refunds"))


class PaymentAllocation(AppendOnlyRecord):
    """Applies part of a payment to an invoice. No ledger effect: the payment already credited AR."""

    payment = models.ForeignKey(CustomerPayment, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocated_on = models.DateField()

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(amount__gt=0), name="payment_allocation_positive")]


class PaymentRefund(AppendOnlyRecord):
    """Returns unapplied customer credit: debit AR, credit cash."""

    payment = models.ForeignKey(CustomerPayment, on_delete=models.PROTECT, related_name="refunds")
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    refunded_on = models.DateField()
    journal_entry = models.OneToOneField("JournalEntry", on_delete=models.PROTECT, related_name="payment_refund")

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(amount__gt=0), name="payment_refund_positive")]


class CreditNote(AppendOnlyRecord):
    """Reduces a posted invoice's open balance: debit sales, credit AR."""

    number = models.CharField(max_length=40, unique=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    issued_on = models.DateField()
    reason = models.CharField(max_length=500)
    journal_entry = models.OneToOneField("JournalEntry", on_delete=models.PROTECT, related_name="credit_note")

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(amount__gt=0), name="credit_note_positive")]


class JournalQuerySet(GuardedQuerySet):
    label = "journal"


class ChartOfAccount(TimeStampedModel):
    class AccountType(models.TextChoices):
        ASSET = "asset", _("Asset")
        LIABILITY = "liability", _("Liability")
        EQUITY = "equity", _("Equity")
        INCOME = "income", _("Income")
        EXPENSE = "expense", _("Expense")

    class NormalBalance(models.TextChoices):
        DEBIT = "debit", _("Debit")
        CREDIT = "credit", _("Credit")

    NORMAL_BALANCES = {
        AccountType.ASSET: NormalBalance.DEBIT,
        AccountType.EXPENSE: NormalBalance.DEBIT,
        AccountType.LIABILITY: NormalBalance.CREDIT,
        AccountType.EQUITY: NormalBalance.CREDIT,
        AccountType.INCOME: NormalBalance.CREDIT,
    }

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=12, choices=AccountType.choices)
    normal_balance = models.CharField(
        max_length=6, choices=NormalBalance.choices, editable=False
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        verbose_name = _("Chart of Account")
        verbose_name_plural = _("Chart of Accounts")

    def clean(self) -> None:
        super().clean()
        expected_balance = self.NORMAL_BALANCES.get(self.account_type)
        if expected_balance and self.normal_balance != expected_balance:
            raise ValidationError(
                {
                    "normal_balance": _(
                        "The normal balance must match the account type."
                    )
                }
            )

    def save(self, *args, **kwargs) -> None:
        self.normal_balance = self.NORMAL_BALANCES[self.account_type]
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class JournalEntry(TimeStampedModel):
    objects = JournalQuerySet.as_manager()

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        POSTED = "posted", _("Posted")
        REVERSED = "reversed", _("Reversed")

    entry_date = models.DateField(default=timezone.localdate)
    memo = models.CharField(max_length=500, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    source_type = models.CharField(max_length=100, blank=True)
    source_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_journal_entries",
    )
    reversal_of = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal_entry",
    )

    class Meta:
        ordering = ["-entry_date", "-created_at"]

    def save(self, *args, **kwargs) -> None:
        if self.pk:
            original_status = (
                JournalEntry.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if original_status in (self.Status.POSTED, self.Status.REVERSED):
                raise ValidationError("Posted journal entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs) -> None:
        if JournalEntry.objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("Posted journal entries cannot be deleted.")
        super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.entry_date} — {self.memo or self.id}"


class JournalLine(TimeStampedModel):
    objects = JournalQuerySet.as_manager()

    entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, related_name="lines"
    )
    account = models.ForeignKey(
        ChartOfAccount, on_delete=models.PROTECT, related_name="journal_lines"
    )
    description = models.CharField(max_length=500, blank=True)
    debit = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    credit = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    currency = models.CharField(max_length=3, default=settings.ACCOUNTING_BASE_CURRENCY)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="accounting_line_amounts_are_non_negative",
            )
        ]

    def clean(self) -> None:
        super().clean()
        debit = self.debit or Decimal("0.00")
        credit = self.credit or Decimal("0.00")
        if (debit > 0) == (credit > 0):
            raise ValidationError(
                _("A journal line must have either a debit or a credit amount.")
            )
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValidationError({"currency": _("Use a three-letter currency code.")})

    def save(self, *args, **kwargs) -> None:
        original_entry_id = JournalLine.objects.filter(pk=self.pk).values_list(
            "entry_id", flat=True
        ).first()
        if JournalEntry.objects.filter(
            pk__in=[self.entry_id, original_entry_id]
        ).exclude(status=JournalEntry.Status.DRAFT).exists():
            raise ValidationError("Lines belonging to posted entries are immutable.")
        self.currency = self.currency.upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs) -> None:
        original_entry_id = JournalLine.objects.filter(pk=self.pk).values_list(
            "entry_id", flat=True
        ).first()
        if JournalEntry.objects.filter(
            pk=original_entry_id
        ).exclude(status=JournalEntry.Status.DRAFT).exists():
            raise ValidationError("Lines belonging to posted entries cannot be deleted.")
        super().delete(*args, **kwargs)
