"""Accounts payable: suppliers, bills with approval, supplier payments and allocations (Goal 5).

Check constraints keep each bill's lifecycle fields consistent and stop the creator
from approving their own bill. Triggers reject writes that bypass the services:
bills move draft -> approved -> posted, or to cancelled; a posting needs its posted
journal entry and a cancellation of a posted bill needs an entry reversing it and
no allocations. Lines of non-draft bills are immutable. Supplier payments and
allocations are insert-only and checked against the ledger and the open and
unapplied balances. PostgreSQL triggers lock parent rows (payment before bill).
"""

import importlib
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import uuid

AR_0006 = importlib.import_module("core_apps.accounting.migrations.0006_ar_settlements")

HEADER = ["id", "number", "supplier_id", "bill_date", "due_date", "currency", "created_by_id", "created_at"]
APPROVAL = ["approved_by_id", "approved_at"]
POSTING = ["posted_total", "journal_entry_id"]
PAYMENT_OK = AR_0006.entry_matches(["supplier_payment"], "2000", "1000")


def same(columns, equal):
    return " AND ".join(f"NEW.{column} {equal} OLD.{column}" for column in columns)


def bill_update_ok(equal):
    """SQL: the bill update is a lifecycle step the services make."""
    return f"""(
        (OLD.status = 'draft' AND NEW.status IN ('draft', 'approved') AND NEW.journal_entry_id IS NULL)
        OR (OLD.status IN ('draft', 'approved') AND NEW.status = 'cancelled' AND NEW.journal_entry_id IS NULL
            AND {same(HEADER, equal)})
        OR (OLD.status = 'approved' AND NEW.status = 'posted' AND {same(HEADER + APPROVAL, equal)}
            AND EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.journal_entry_id
                        AND e.status = 'posted' AND e.source_type = 'bill' AND e.source_id = NEW.id
                        AND EXISTS (SELECT 1 FROM accounting_journalline l JOIN accounting_chartofaccount a
                                    ON a.id = l.account_id WHERE l.entry_id = e.id AND a.code = '2000'
                                    AND l.credit = NEW.posted_total)))
        OR (OLD.status = 'posted' AND NEW.status = 'cancelled' AND {same(HEADER + APPROVAL + POSTING, equal)}
            AND EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.cancellation_entry_id
                        AND e.status = 'posted' AND e.reversal_of_id = OLD.journal_entry_id)
            AND NOT EXISTS (SELECT 1 FROM accounting_supplierpaymentallocation WHERE bill_id = OLD.id)))"""


def bill_open(bill):
    return f"{bill}.posted_total - {AR_0006.sum_of('accounting_supplierpaymentallocation', 'bill_id', f'{bill}.id')}"


def payment_unapplied(payment):
    return (f"{payment}.amount - "
            f"{AR_0006.sum_of('accounting_supplierpaymentallocation', 'payment_id', f'{payment}.id')}")


ALLOCATION_OK = f"""EXISTS (
    SELECT 1 FROM accounting_supplierpayment p JOIN accounting_bill b ON b.id = NEW.bill_id
    WHERE p.id = NEW.payment_id AND b.status = 'posted' AND b.supplier_id = p.supplier_id
    AND b.currency = p.currency AND NEW.allocated_on >= p.paid_on AND NEW.allocated_on >= b.bill_date
    AND NEW.amount <= {bill_open('b')} AND NEW.amount <= {payment_unapplied('p')})"""
ALLOCATION_MESSAGE = "Allocation requires a matching posted bill and enough open and unapplied balance."
PAYMENT_MESSAGE = "Supplier payment requires a posted entry debiting AP and crediting cash."
LINE_MESSAGE = "Lines of approved or posted bills are immutable."

SQLITE_FORWARD = [
    f"""
    CREATE TRIGGER accounting_bill_guard_insert BEFORE INSERT ON accounting_bill
    WHEN NEW.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Bills must be created as drafts.'); END
    """,
    f"""
    CREATE TRIGGER accounting_bill_guard_update BEFORE UPDATE ON accounting_bill
    WHEN NOT {bill_update_ok("IS")}
    BEGIN SELECT RAISE(ABORT, 'Bills change only through approval, posting and cancellation.'); END
    """,
    """
    CREATE TRIGGER accounting_bill_guard_delete BEFORE DELETE ON accounting_bill
    WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Only draft bills can be deleted.'); END
    """,
    f"""
    CREATE TRIGGER accounting_billline_guard_insert BEFORE INSERT ON accounting_billline
    WHEN (SELECT status FROM accounting_bill WHERE id = NEW.bill_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{LINE_MESSAGE}'); END
    """,
    f"""
    CREATE TRIGGER accounting_billline_guard_update BEFORE UPDATE ON accounting_billline
    WHEN EXISTS (SELECT 1 FROM accounting_bill WHERE id IN (OLD.bill_id, NEW.bill_id) AND status <> 'draft')
    BEGIN SELECT RAISE(ABORT, '{LINE_MESSAGE}'); END
    """,
    f"""
    CREATE TRIGGER accounting_billline_guard_delete BEFORE DELETE ON accounting_billline
    WHEN (SELECT status FROM accounting_bill WHERE id = OLD.bill_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{LINE_MESSAGE}'); END
    """,
    f"""
    CREATE TRIGGER accounting_supplierpayment_guard_insert BEFORE INSERT ON accounting_supplierpayment
    WHEN NOT {PAYMENT_OK}
    BEGIN SELECT RAISE(ABORT, '{PAYMENT_MESSAGE}'); END
    """,
    f"""
    CREATE TRIGGER accounting_supplierpaymentallocation_guard_insert BEFORE INSERT
    ON accounting_supplierpaymentallocation WHEN NOT {ALLOCATION_OK}
    BEGIN SELECT RAISE(ABORT, '{ALLOCATION_MESSAGE}'); END
    """,
] + [
    f"""
    CREATE TRIGGER accounting_{table}_guard_{operation} BEFORE {operation.upper()} ON accounting_{table}
    BEGIN SELECT RAISE(ABORT, 'Settlement records are immutable.'); END
    """
    for table in ("supplierpayment", "supplierpaymentallocation") for operation in ("update", "delete")
]
SQLITE_REVERSE = [f"DROP TRIGGER IF EXISTS accounting_{table}_guard_{operation}"
                  for table in ("bill", "billline", "supplierpayment", "supplierpaymentallocation")
                  for operation in ("insert", "update", "delete")]

POSTGRES_FORWARD = [
    f"""
    CREATE FUNCTION accounting_bill_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'draft' THEN
                RAISE EXCEPTION 'Bills must be created as drafts.' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            IF OLD.status <> 'draft' THEN
                RAISE EXCEPTION 'Only draft bills can be deleted.' USING ERRCODE = 'check_violation';
            END IF;
            RETURN OLD;
        END IF;
        IF NOT {bill_update_ok("IS NOT DISTINCT FROM")} THEN
            RAISE EXCEPTION 'Bills change only through approval, posting and cancellation.'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_bill_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_bill
    FOR EACH ROW EXECUTE FUNCTION accounting_bill_guard()
    """,
    f"""
    CREATE FUNCTION accounting_billline_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        parent_status text;
    BEGIN
        -- Lock every parent row before reading its status, so a concurrent approval is seen.
        FOR parent_status IN
            SELECT status FROM accounting_bill
            WHERE id IN (CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.bill_id END,
                         CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.bill_id END)
            ORDER BY id FOR SHARE
        LOOP
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION '{LINE_MESSAGE}' USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_billline_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_billline
    FOR EACH ROW EXECUTE FUNCTION accounting_billline_guard()
    """,
] + AR_0006.postgres_guard(
    "supplierpayment", AR_0006.postgres_reject(PAYMENT_OK, PAYMENT_MESSAGE),
) + AR_0006.postgres_guard(
    "supplierpaymentallocation",
    # Lock order matches allocate_supplier_payment: payment, then bill.
    "PERFORM 1 FROM accounting_supplierpayment WHERE id = NEW.payment_id FOR UPDATE;"
    " PERFORM 1 FROM accounting_bill WHERE id = NEW.bill_id FOR UPDATE;"
    + AR_0006.postgres_reject(ALLOCATION_OK, ALLOCATION_MESSAGE),
)
POSTGRES_REVERSE = [statement for table in ("bill", "billline", "supplierpayment", "supplierpaymentallocation")
                    for statement in (f"DROP TRIGGER IF EXISTS accounting_{table}_guard ON accounting_{table}",
                                      f"DROP FUNCTION IF EXISTS accounting_{table}_guard()")]
STATEMENTS = {"sqlite": (SQLITE_FORWARD, SQLITE_REVERSE), "postgresql": (POSTGRES_FORWARD, POSTGRES_REVERSE)}


def execute(direction):
    def run(apps, schema_editor):
        for statement in STATEMENTS[schema_editor.connection.vendor][direction]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounting', '0011_audit_events'),
    ]

    operations = [
        migrations.CreateModel(
            name='Bill',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('number', models.CharField(max_length=60)),
                ('bill_date', models.DateField()),
                ('due_date', models.DateField()),
                ('currency', models.CharField(default='EUR', max_length=3)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('approved', 'Approved'), ('posted', 'Posted'), ('cancelled', 'Cancelled')], default='draft', max_length=10)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('posted_total', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='bills_approved', to=settings.AUTH_USER_MODEL)),
                ('cancellation_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='cancelled_bill', to='accounting.journalentry')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='bills_created', to=settings.AUTH_USER_MODEL)),
                ('journal_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='bill', to='accounting.journalentry')),
            ],
            options={
                'permissions': [('approve_bill', 'Can approve bills'), ('post_bill', 'Can post bills'), ('cancel_bill', 'Can cancel bills')],
            },
        ),
        migrations.CreateModel(
            name='Supplier',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=200)),
                ('email', models.EmailField(blank=True, max_length=254)),
                ('is_active', models.BooleanField(default=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SupplierPayment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reference', models.CharField(max_length=100, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('currency', models.CharField(default='EUR', max_length=3)),
                ('paid_on', models.DateField()),
                ('journal_entry', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='supplier_payment', to='accounting.journalentry')),
                ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='accounting.supplier')),
            ],
        ),
        migrations.CreateModel(
            name='SupplierPaymentAllocation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reference', models.CharField(max_length=100, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('allocated_on', models.DateField()),
                ('bill', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocations', to='accounting.bill')),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='allocations', to='accounting.supplierpayment')),
            ],
        ),
        migrations.CreateModel(
            name='BillLine',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('description', models.CharField(max_length=500)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.001'))])),
                ('unit_price', models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))])),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bill_lines', to='accounting.chartofaccount')),
                ('bill', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='accounting.bill')),
            ],
        ),
        migrations.AddField(
            model_name='bill',
            name='supplier',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bills', to='accounting.supplier'),
        ),
        migrations.AddConstraint(
            model_name='supplierpaymentallocation',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='supplier_allocation_positive'),
        ),
        migrations.AddConstraint(
            model_name='supplierpayment',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='supplier_payment_positive'),
        ),
        migrations.AddConstraint(
            model_name='supplierpayment',
            constraint=models.CheckConstraint(check=models.Q(('currency__in', ('EUR',))), name='supplier_payment_currency_supported'),
        ),
        migrations.AddConstraint(
            model_name='billline',
            constraint=models.CheckConstraint(check=models.Q(('quantity__gt', 0)), name='bill_quantity_positive'),
        ),
        migrations.AddConstraint(
            model_name='billline',
            constraint=models.CheckConstraint(check=models.Q(('unit_price__gte', 0)), name='bill_price_nonnegative'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.UniqueConstraint(fields=('supplier', 'number'), name='bill_number_unique_per_supplier'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.CheckConstraint(check=models.Q(('due_date__gte', models.F('bill_date'))), name='bill_due_not_before_bill_date'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.CheckConstraint(check=models.Q(('currency__in', ('EUR',))), name='bill_currency_supported'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('approved_at__isnull', True), ('approved_by__isnull', True), ('cancellation_entry__isnull', True), ('journal_entry__isnull', True), ('posted_total__isnull', True), ('status', 'draft')), models.Q(('approved_at__isnull', False), ('approved_by__isnull', False), ('cancellation_entry__isnull', True), ('journal_entry__isnull', True), ('posted_total__isnull', True), ('status', 'approved')), models.Q(('approved_at__isnull', False), ('approved_by__isnull', False), ('cancellation_entry__isnull', True), ('journal_entry__isnull', False), ('posted_total__gt', 0), ('status', 'posted')), models.Q(('cancellation_entry__isnull', True), ('journal_entry__isnull', True), ('posted_total__isnull', True), ('status', 'cancelled')), models.Q(('approved_by__isnull', False), ('cancellation_entry__isnull', False), ('journal_entry__isnull', False), ('posted_total__gt', 0), ('status', 'cancelled')), _connector='OR'), name='bill_lifecycle_consistent'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.CheckConstraint(check=models.Q(('approved_by__isnull', True), ('created_by__isnull', True), models.Q(('approved_by', models.F('created_by')), _negated=True), _connector='OR'), name='bill_approver_is_not_creator'),
        ),
        migrations.RunPython(execute(0), execute(1)),
    ]
