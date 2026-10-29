"""Read-only admin for the accounting app: a browsable back office.

Every write goes through the services (locks, validation, audit), so the admin
never adds, changes or deletes anything and has no bulk actions. Viewing needs
the model's `view_*` permission, as in the API.
"""

from django.contrib import admin
from django.db.models import Sum

from .models import (AllocationRelease, AuditEvent, BankMatch, BankStatement, BankStatementLine, Bill, BillLine,
                     ChartOfAccount, CreditNote, Customer, CustomerPayment, Invoice, InvoiceLine, JournalEntry,
                     JournalLine, PaymentAllocation, PaymentRefund, PeriodClose, StripeBalanceTransaction,
                     StripeCheckoutSession, StripeWebhookEvent, Supplier, SupplierPayment,
                     SupplierPaymentAllocation)


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(ReadOnlyMixin, admin.ModelAdmin):
    actions = None
    list_per_page = 50
    date_hierarchy = None


class ReadOnlyInline(ReadOnlyMixin, admin.TabularInline):
    extra = 0
    show_change_link = True


# Ledger

@admin.register(ChartOfAccount)
class ChartOfAccountAdmin(ReadOnlyAdmin):
    list_display = ["code", "name", "account_type", "normal_balance", "is_active"]
    list_filter = ["account_type", "is_active"]
    search_fields = ["code", "name"]


class JournalLineInline(ReadOnlyInline):
    model = JournalLine
    fields = ["account", "description", "debit", "credit", "currency"]
    show_change_link = False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("account")


@admin.register(JournalEntry)
class JournalEntryAdmin(ReadOnlyAdmin):
    list_display = ["entry_date", "memo", "status", "source_type", "total", "posted_by", "posted_at"]
    list_filter = ["status", "source_type", "entry_date"]
    search_fields = ["memo", "reference", "source_id"]
    list_select_related = ["posted_by"]
    date_hierarchy = "entry_date"
    inlines = [JournalLineInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(total_debit=Sum("lines__debit"))

    @admin.display(description="Total", ordering="total_debit")
    def total(self, obj):
        return obj.total_debit


@admin.register(JournalLine)
class JournalLineAdmin(ReadOnlyAdmin):
    list_display = ["entry", "account", "debit", "credit", "currency", "description"]
    list_filter = ["account", "entry__status"]
    search_fields = ["description", "entry__memo", "account__code"]
    list_select_related = ["entry", "account"]


@admin.register(PeriodClose)
class PeriodCloseAdmin(ReadOnlyAdmin):
    list_display = ["closed_through", "closing_entry", "closed_by", "created_at"]
    list_select_related = ["closing_entry", "closed_by"]


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "action", "summary", "actor"]
    list_filter = ["action", "object_type"]
    search_fields = ["summary", "object_id", "action"]
    list_select_related = ["actor"]
    date_hierarchy = "created_at"


# Receivables

@admin.register(Customer)
class CustomerAdmin(ReadOnlyAdmin):
    list_display = ["name", "email", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "email"]


class InvoiceLineInline(ReadOnlyInline):
    model = InvoiceLine
    fields = ["description", "quantity", "unit_price"]
    show_change_link = False


class InvoiceAllocationInline(ReadOnlyInline):
    model = PaymentAllocation
    fk_name = "invoice"
    fields = ["payment", "reference", "amount", "allocated_on"]


class CreditNoteInline(ReadOnlyInline):
    model = CreditNote
    fields = ["number", "amount", "issued_on", "reason"]


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyAdmin):
    list_display = ["number", "customer", "issue_date", "due_date", "status", "posted_total", "open_balance"]
    list_filter = ["status", "issue_date"]
    search_fields = ["number", "customer__name"]
    list_select_related = ["customer"]
    date_hierarchy = "issue_date"
    inlines = [InvoiceLineInline, InvoiceAllocationInline, CreditNoteInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_balances()

    @admin.display(description="Open balance")
    def open_balance(self, obj):
        return obj.open_balance


class PaymentAllocationInline(ReadOnlyInline):
    model = PaymentAllocation
    fk_name = "payment"
    fields = ["invoice", "reference", "amount", "allocated_on"]


class PaymentRefundInline(ReadOnlyInline):
    model = PaymentRefund
    fields = ["reference", "amount", "refunded_on", "journal_entry"]


@admin.register(CustomerPayment)
class CustomerPaymentAdmin(ReadOnlyAdmin):
    list_display = ["reference", "customer", "amount", "currency", "received_on", "unapplied_amount"]
    list_filter = ["received_on"]
    search_fields = ["reference", "customer__name"]
    list_select_related = ["customer"]
    date_hierarchy = "received_on"
    inlines = [PaymentAllocationInline, PaymentRefundInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_balances()

    @admin.display(description="Unapplied")
    def unapplied_amount(self, obj):
        return obj.unapplied_amount


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = ["reference", "payment", "invoice", "amount", "allocated_on", "released"]
    search_fields = ["reference", "invoice__number", "payment__reference"]
    list_select_related = ["payment", "invoice", "release"]

    @admin.display(boolean=True)
    def released(self, obj):
        return hasattr(obj, "release")


@admin.register(AllocationRelease)
class AllocationReleaseAdmin(ReadOnlyAdmin):
    list_display = ["allocation", "released_on", "reason", "released_by"]
    list_select_related = ["allocation", "released_by"]


@admin.register(PaymentRefund)
class PaymentRefundAdmin(ReadOnlyAdmin):
    list_display = ["reference", "payment", "amount", "refunded_on"]
    search_fields = ["reference", "payment__reference"]
    list_select_related = ["payment"]


@admin.register(CreditNote)
class CreditNoteAdmin(ReadOnlyAdmin):
    list_display = ["number", "invoice", "amount", "issued_on", "reason"]
    search_fields = ["number", "invoice__number"]
    list_select_related = ["invoice"]


# Stripe

@admin.register(StripeCheckoutSession)
class StripeCheckoutSessionAdmin(ReadOnlyAdmin):
    list_display = ["invoice", "status", "amount", "currency", "created_at", "expires_at", "payment"]
    list_filter = ["status"]
    search_fields = ["stripe_session_id", "payment_intent_id", "invoice__number"]
    list_select_related = ["invoice", "payment"]


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(ReadOnlyAdmin):
    list_display = ["created_at", "event_type", "status", "attempts", "livemode", "event_id"]
    list_filter = ["status", "event_type", "livemode"]
    search_fields = ["event_id", "event_type"]


@admin.register(StripeBalanceTransaction)
class StripeBalanceTransactionAdmin(ReadOnlyAdmin):
    list_display = ["stripe_created", "reporting_category", "amount", "fee", "net", "state", "stripe_id"]
    list_filter = ["state", "reporting_category"]
    search_fields = ["stripe_id", "source_id"]


# Bank

class BankStatementLineInline(ReadOnlyInline):
    model = BankStatementLine
    fields = ["sequence", "booked_on", "amount", "description", "bank_reference"]


@admin.register(BankStatement)
class BankStatementAdmin(ReadOnlyAdmin):
    list_display = ["reference", "account", "start_date", "end_date", "opening_balance", "closing_balance"]
    search_fields = ["reference"]
    list_select_related = ["account"]
    inlines = [BankStatementLineInline]


@admin.register(BankStatementLine)
class BankStatementLineAdmin(ReadOnlyAdmin):
    list_display = ["booked_on", "amount", "description", "bank_reference", "statement", "matched"]
    search_fields = ["description", "bank_reference", "statement__reference"]
    list_select_related = ["statement", "match"]

    @admin.display(boolean=True)
    def matched(self, obj):
        return hasattr(obj, "match")


@admin.register(BankMatch)
class BankMatchAdmin(ReadOnlyAdmin):
    list_display = ["statement_line", "journal_line", "method", "matched_by", "created_at"]
    list_filter = ["method"]
    list_select_related = ["statement_line", "journal_line__account", "matched_by"]


# Payables

@admin.register(Supplier)
class SupplierAdmin(ReadOnlyAdmin):
    list_display = ["name", "email", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "email"]


class BillLineInline(ReadOnlyInline):
    model = BillLine
    fields = ["account", "description", "quantity", "unit_price"]
    show_change_link = False


class BillAllocationInline(ReadOnlyInline):
    model = SupplierPaymentAllocation
    fk_name = "bill"
    fields = ["payment", "reference", "amount", "allocated_on"]


@admin.register(Bill)
class BillAdmin(ReadOnlyAdmin):
    list_display = ["number", "supplier", "bill_date", "due_date", "status", "posted_total", "open_balance",
                    "created_by", "approved_by"]
    list_filter = ["status", "bill_date"]
    search_fields = ["number", "supplier__name"]
    list_select_related = ["supplier", "created_by", "approved_by"]
    date_hierarchy = "bill_date"
    inlines = [BillLineInline, BillAllocationInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_balances()

    @admin.display(description="Open balance")
    def open_balance(self, obj):
        return obj.open_balance


class SupplierAllocationInline(ReadOnlyInline):
    model = SupplierPaymentAllocation
    fk_name = "payment"
    fields = ["bill", "reference", "amount", "allocated_on"]


@admin.register(SupplierPayment)
class SupplierPaymentAdmin(ReadOnlyAdmin):
    list_display = ["reference", "supplier", "amount", "currency", "paid_on", "unapplied_amount"]
    search_fields = ["reference", "supplier__name"]
    list_select_related = ["supplier"]
    date_hierarchy = "paid_on"
    inlines = [SupplierAllocationInline]

    def get_queryset(self, request):
        return super().get_queryset(request).with_balances()

    @admin.display(description="Unapplied")
    def unapplied_amount(self, obj):
        return obj.unapplied_amount


@admin.register(SupplierPaymentAllocation)
class SupplierPaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = ["reference", "payment", "bill", "amount", "allocated_on"]
    search_fields = ["reference", "bill__number", "payment__reference"]
    list_select_related = ["payment", "bill"]
