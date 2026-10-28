from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core_apps.common.models import TimeStampedModel

User = get_user_model()


class Customer(TimeStampedModel):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Invoice(TimeStampedModel):
    """Draft document only; ledger posting is a subsequent implementation stage."""

    number = models.CharField(max_length=40, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    currency = models.CharField(max_length=3, default="USD")

    def clean(self):
        super().clean()
        if self.issue_date and self.due_date and self.due_date < self.issue_date:
            raise ValidationError({"due_date": "Due date must not precede issue date."})
        if self.currency not in ("USD", "EUR", "GBP"):
            raise ValidationError({"currency": "Draft invoices currently support USD, EUR and GBP."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def total(self):
        # Round each line first, then sum; this matches displayed document lines.
        return sum((line.total for line in self.lines.all()), Decimal("0.00"))


class InvoiceLine(TimeStampedModel):
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

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def total(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class JournalQuerySet(models.QuerySet):
    """Bulk writes bypass instance validation; require validated instance writes."""

    def update(self, **kwargs):
        raise ValidationError("Bulk journal updates are prohibited.")

    def delete(self):
        raise ValidationError("Bulk journal deletion is prohibited.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Bulk journal updates are prohibited.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Bulk journal creation is prohibited.")


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
