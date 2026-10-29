from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core_apps.common.models import TimeStampedModel

User = get_user_model()
# The ledger has one currency: EUR, the Stripe account's settlement currency. Adding
# another needs exchange rates and FX gain/loss accounting (MONEY-01).
BASE_CURRENCY = "EUR"
INVOICE_CURRENCIES = (BASE_CURRENCY,)


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
    currency = models.CharField(max_length=3, default=BASE_CURRENCY)
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
            raise ValidationError({"currency": f"Invoices must use {BASE_CURRENCY}."})

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


class StripeCheckoutSession(TimeStampedModel):
    """A Stripe Checkout Session for an invoice's open balance when it was created.

    Webhooks, not the success page, move it to paid and record the payment.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        AWAITING_PAYMENT = "awaiting_payment", _("Completed, awaiting asynchronous payment")
        PAID = "paid", _("Paid")
        FAILED = "failed", _("Payment failed")
        EXPIRED = "expired", _("Expired")

    ACTIVE = [Status.OPEN, Status.AWAITING_PAYMENT]

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="stripe_sessions")
    stripe_session_id = models.CharField(max_length=255, unique=True)
    url = models.URLField(max_length=2000)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    expires_at = models.DateTimeField()
    payment_intent_id = models.CharField(max_length=255, blank=True)
    payment = models.OneToOneField(CustomerPayment, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="stripe_session")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="stripe_checkout_sessions")

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name="stripe_session_amount_positive"),
            models.UniqueConstraint(fields=["invoice"], condition=models.Q(status__in=["open", "awaiting_payment"]),
                                    name="stripe_session_one_active_per_invoice"),
            models.CheckConstraint(check=models.Q(status="paid", payment__isnull=False)
                                   | (~models.Q(status="paid") & models.Q(payment__isnull=True)),
                                   name="stripe_session_paid_has_payment"),
        ]


class StripeWebhookEvent(TimeStampedModel):
    """Each verified Stripe event is stored once; replays of a finished event change nothing."""

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        IGNORED = "ignored", _("Ignored")
        FAILED = "failed", _("Failed")

    FINISHED = [Status.PROCESSED, Status.IGNORED]

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    livemode = models.BooleanField()
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)


class StripeBalanceTransaction(TimeStampedModel):
    """A movement of the Stripe balance, stored once and reconciled against Stripe clearing (1010).

    Amounts are signed as Stripe reports them: net = amount - fee is the balance change.
    Items that need review post nothing and are retried on every sync.
    """

    class State(models.TextChoices):
        POSTED = "posted", _("Posted")
        NOTHING_TO_POST = "nothing_to_post", _("Matched, nothing to post")
        NEEDS_REVIEW = "needs_review", _("Needs review")

    FINISHED = [State.POSTED, State.NOTHING_TO_POST]

    stripe_id = models.CharField(max_length=255, unique=True)
    reporting_category = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    fee = models.DecimalField(max_digits=18, decimal_places=2)
    net = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    stripe_status = models.CharField(max_length=20)
    stripe_created = models.DateTimeField()
    source_id = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.NEEDS_REVIEW)
    review_reason = models.CharField(max_length=500, blank=True)
    payment = models.ForeignKey(CustomerPayment, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="stripe_balance_transactions")
    refund = models.OneToOneField(PaymentRefund, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="stripe_balance_transaction")
    journal_entry = models.OneToOneField("JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="stripe_balance_transaction")
    payload = models.JSONField()

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(net=models.F("amount") - models.F("fee")),
                                   name="stripe_balance_net_is_amount_less_fee"),
            models.CheckConstraint(check=~models.Q(state="posted")
                                   | models.Q(journal_entry__isnull=False) | models.Q(refund__isnull=False),
                                   name="stripe_balance_posted_has_entry"),
            models.CheckConstraint(check=models.Q(state="posted")
                                   | models.Q(journal_entry__isnull=True, refund__isnull=True),
                                   name="stripe_balance_only_posted_has_entry"),
        ]


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
        original = ChartOfAccount.objects.filter(pk=self.pk).values("code", "account_type").first()
        if original and original != {"code": self.code, "account_type": self.account_type} and (
                self.journal_lines.exclude(entry__status=JournalEntry.Status.DRAFT).exists()):
            # Changing them would reinterpret posted history; rename or deactivate instead.
            raise ValidationError("The code and type of an account with posted lines cannot change.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class JournalEntry(TimeStampedModel):
    objects = JournalQuerySet.as_manager()

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        POSTED = "posted", _("Posted")
        REVERSED = "reversed", _("Reversed")

    # Statuses whose lines count in balances; a reversal is offset by its correcting entry.
    IN_LEDGER = [Status.POSTED, Status.REVERSED]

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

    def _mark_reversed(self):
        """Service-only: called by reverse_journal_entry after posting the reversal; triggers verify it."""
        self.status = self.Status.REVERSED
        super().save(update_fields=["status", "updated_at"])

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
    currency = models.CharField(max_length=3, default=BASE_CURRENCY)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="accounting_line_amounts_are_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(currency=BASE_CURRENCY), name="accounting_line_base_currency"
            ),
            models.CheckConstraint(
                check=models.Q(debit__gt=0, credit=0) | models.Q(debit=0, credit__gt=0),
                name="accounting_line_one_side",
            ),
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

    @transaction.atomic
    def save(self, *args, **kwargs) -> None:
        original_entry_id = JournalLine.objects.filter(pk=self.pk).values_list(
            "entry_id", flat=True
        ).first()
        # Lock the parent entries like post_journal_entry, so posting and line edits serialize.
        if JournalEntry.objects.select_for_update().filter(
            pk__in=[self.entry_id, original_entry_id]
        ).exclude(status=JournalEntry.Status.DRAFT).exists():
            raise ValidationError("Lines belonging to posted entries are immutable.")
        self.currency = self.currency.upper()
        self.full_clean()
        super().save(*args, **kwargs)

    @transaction.atomic
    def delete(self, *args, **kwargs) -> None:
        original_entry_id = JournalLine.objects.filter(pk=self.pk).values_list(
            "entry_id", flat=True
        ).first()
        if JournalEntry.objects.select_for_update().filter(
            pk=original_entry_id
        ).exclude(status=JournalEntry.Status.DRAFT).exists():
            raise ValidationError("Lines belonging to posted entries cannot be deleted.")
        super().delete(*args, **kwargs)


class BankQuerySet(GuardedQuerySet):
    label = "bank statement"


class BankStatement(AppendOnlyRecord):
    """An imported statement of a bank account. Statements of one account form a chain:
    each starts after the previous one ends, with its closing balance as opening balance."""

    objects = BankQuerySet.as_manager()

    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="bank_statements")
    reference = models.CharField(max_length=100, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2)
    closing_balance = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default=BASE_CURRENCY)
    imported_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="bank_statements")

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(end_date__gte=models.F("start_date")),
                                   name="bank_statement_period_valid"),
            models.CheckConstraint(check=models.Q(currency__in=INVOICE_CURRENCIES),
                                   name="bank_statement_currency_supported"),
        ]


class BankStatementLine(AppendOnlyRecord):
    """One bank movement: positive amounts are money in, negative money out."""

    objects = BankQuerySet.as_manager()

    statement = models.ForeignKey(BankStatement, on_delete=models.PROTECT, related_name="lines")
    sequence = models.PositiveIntegerField()
    booked_on = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    description = models.CharField(max_length=500, blank=True)
    bank_reference = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["booked_on", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["statement", "sequence"], name="bank_line_sequence_unique"),
            models.CheckConstraint(check=~models.Q(amount=0), name="bank_line_amount_nonzero"),
        ]


class BankMatch(TimeStampedModel):
    """Pairs a statement line with the posted ledger line for the same movement of the bank account.
    Deleting a match unmatches both; the statement and the ledger stay unchanged."""

    class Method(models.TextChoices):
        AUTOMATIC = "automatic", _("Automatic")
        MANUAL = "manual", _("Manual")
        POSTED = "posted", _("Posted from the statement")

    objects = BankQuerySet.as_manager()

    statement_line = models.OneToOneField(BankStatementLine, on_delete=models.PROTECT, related_name="match")
    journal_line = models.OneToOneField("JournalLine", on_delete=models.PROTECT, related_name="bank_match")
    method = models.CharField(max_length=10, choices=Method.choices)
    matched_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="bank_matches")

    def save(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Bank matches cannot be changed; unmatch and match again.")
        return super().save(*args, **kwargs)


class AuditQuerySet(GuardedQuerySet):
    label = "audit event"


class AuditEvent(AppendOnlyRecord):
    """Who changed what in the accounting records, written by the services in the same transaction.

    Replays of idempotent requests change nothing and record nothing.
    """

    objects = AuditQuerySet.as_manager()

    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=64)
    summary = models.CharField(max_length=500)
    details = models.JSONField(default=dict, blank=True)
    # PROTECT: audit rows are never updated, so a user with history cannot be deleted (deactivate instead).
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [models.Index(fields=["object_type", "object_id"], name="audit_event_object"),
                   models.Index(fields=["created_at"], name="audit_event_created")]


class APQuerySet(GuardedQuerySet):
    label = "AP document"


class BillQuerySet(APQuerySet):
    def with_balances(self):
        return self.annotate(allocated_total=_amount_sum(SupplierPaymentAllocation, "bill"))


class SupplierPaymentQuerySet(APQuerySet):
    def with_balances(self):
        return self.annotate(allocated_total=_amount_sum(SupplierPaymentAllocation, "payment"))


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Bill(TimeStampedModel):
    """A supplier's invoice. Drafts are approved by someone other than their creator, then posted.

    Lifecycle changes go through services; database triggers reject other writes.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        APPROVED = "approved", _("Approved")
        POSTED = "posted", _("Posted")
        CANCELLED = "cancelled", _("Cancelled")

    objects = BillQuerySet.as_manager()

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="bills")
    number = models.CharField(max_length=60)  # The supplier's invoice number.
    bill_date = models.DateField()
    due_date = models.DateField()
    currency = models.CharField(max_length=3, default=BASE_CURRENCY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="bills_created")
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="bills_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    journal_entry = models.OneToOneField("JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="bill")
    cancellation_entry = models.OneToOneField("JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                              related_name="cancelled_bill")

    class Meta:
        permissions = [("approve_bill", "Can approve bills"), ("post_bill", "Can post bills"),
                       ("cancel_bill", "Can cancel bills")]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "number"], name="bill_number_unique_per_supplier"),
            models.CheckConstraint(check=models.Q(due_date__gte=models.F("bill_date")),
                                   name="bill_due_not_before_bill_date"),
            models.CheckConstraint(check=models.Q(currency__in=INVOICE_CURRENCIES), name="bill_currency_supported"),
            models.CheckConstraint(name="bill_lifecycle_consistent", check=(
                models.Q(status="draft", approved_by__isnull=True, approved_at__isnull=True,
                         journal_entry__isnull=True, posted_total__isnull=True, cancellation_entry__isnull=True)
                | models.Q(status="approved", approved_by__isnull=False, approved_at__isnull=False,
                           journal_entry__isnull=True, posted_total__isnull=True, cancellation_entry__isnull=True)
                | models.Q(status="posted", approved_by__isnull=False, approved_at__isnull=False,
                           journal_entry__isnull=False, posted_total__gt=0, cancellation_entry__isnull=True)
                | models.Q(status="cancelled", journal_entry__isnull=True, posted_total__isnull=True,
                           cancellation_entry__isnull=True)
                | models.Q(status="cancelled", approved_by__isnull=False, journal_entry__isnull=False,
                           posted_total__gt=0, cancellation_entry__isnull=False))),
            models.CheckConstraint(check=models.Q(approved_by__isnull=True) | models.Q(created_by__isnull=True)
                                   | ~models.Q(approved_by=models.F("created_by")),
                                   name="bill_approver_is_not_creator"),
        ]

    def clean(self):
        super().clean()
        if self.bill_date and self.due_date and self.due_date < self.bill_date:
            raise ValidationError({"due_date": "Due date must not precede the bill date."})
        if self.currency not in INVOICE_CURRENCIES:
            raise ValidationError({"currency": f"Bills must use {BASE_CURRENCY}."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        original = Bill.objects.select_for_update().filter(pk=self.pk).first()
        if original and original.status != "draft":
            raise ValidationError("Approved or posted bills must be changed through accounting services.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def _apply_transition(self, **fields):
        """Service-only lifecycle change; the caller holds the row lock and triggers validate it."""
        for name, value in fields.items():
            setattr(self, name, value)
        super().save(update_fields=[*fields, "updated_at"])

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0.00"))

    @property
    def open_balance(self):
        """What is still owed; use Bill.objects.with_balances() to avoid per-row queries."""
        if self.status != "posted":
            return Decimal("0.00")
        return self.posted_total - _stored_total(self, "allocated_total", "allocations")

    def delete(self, *args, **kwargs):
        if Bill.objects.filter(pk=self.pk).exclude(status="draft").exists():
            raise ValidationError("Only draft bills can be deleted.")
        return super().delete(*args, **kwargs)


class BillLine(TimeStampedModel):
    objects = APQuerySet.as_manager()

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name="bill_lines")
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="bill_quantity_positive"),
            models.CheckConstraint(check=models.Q(unit_price__gte=0), name="bill_price_nonnegative"),
        ]

    def _lock_draft_bills(self, *bill_ids):
        statuses = Bill.objects.select_for_update().filter(pk__in=[pk for pk in bill_ids if pk]).values_list(
            "status", flat=True)
        if any(status != "draft" for status in statuses):
            raise ValidationError("Lines of approved or posted bills are immutable.")

    @transaction.atomic
    def save(self, *args, **kwargs):
        original = BillLine.objects.filter(pk=self.pk).values_list("bill_id", flat=True).first()
        self._lock_draft_bills(original, self.bill_id)
        self.full_clean()
        return super().save(*args, **kwargs)

    @transaction.atomic
    def delete(self, *args, **kwargs):
        self._lock_draft_bills(BillLine.objects.filter(pk=self.pk).values_list("bill_id", flat=True).first())
        return super().delete(*args, **kwargs)

    @property
    def total(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SupplierPayment(AppendOnlyRecord):
    """Money paid to a supplier: debit AP, credit cash. Allocations apply it to bills; the rest is a prepayment."""

    objects = SupplierPaymentQuerySet.as_manager()

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default=BASE_CURRENCY)
    paid_on = models.DateField()
    journal_entry = models.OneToOneField("JournalEntry", on_delete=models.PROTECT, related_name="supplier_payment")

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name="supplier_payment_positive"),
            models.CheckConstraint(check=models.Q(currency__in=INVOICE_CURRENCIES),
                                   name="supplier_payment_currency_supported"),
        ]

    @property
    def unapplied_amount(self):
        return self.amount - _stored_total(self, "allocated_total", "allocations")


class SupplierPaymentAllocation(AppendOnlyRecord):
    """Applies part of a supplier payment to a posted bill. No ledger effect."""

    objects = APQuerySet.as_manager()

    payment = models.ForeignKey(SupplierPayment, on_delete=models.PROTECT, related_name="allocations")
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="allocations")
    reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocated_on = models.DateField()

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(amount__gt=0), name="supplier_allocation_positive")]


class PeriodClose(AppendOnlyRecord):
    """Books are closed through `closed_through`: nothing may be posted on or before it.

    Closing is final; corrections are posted in the next open period. A close may also
    have posted a closing entry moving income and expense balances to retained earnings.
    """

    objects = GuardedQuerySet.as_manager()

    closed_through = models.DateField(unique=True)
    closing_entry = models.OneToOneField("JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="period_close")
    closed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="period_closes")

    class Meta:
        ordering = ["-closed_through"]
