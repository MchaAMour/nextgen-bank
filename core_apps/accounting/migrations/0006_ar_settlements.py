"""Reusable customer payments, allocations, refunds and credit notes.

Each manual InvoiceReceipt becomes a CustomerPayment with one PaymentAllocation
that keeps the receipt's reference and journal entry; the receipt table is then
removed. New tables are insert-only. Triggers on SQLite and PostgreSQL check each
insert against the ledger entry and the open invoice and payment balances, and a
posted invoice with allocations or credit notes can no longer be cancelled.
PostgreSQL triggers lock the payment row before the invoice row, the same order
as the services.
"""

import importlib

from django.db import migrations, models
import django.db.models.deletion
import uuid

AR_0005 = importlib.import_module("core_apps.accounting.migrations.0005_ar_document_integrity")


def entry_matches(source_types, debit_code, credit_code):
    """SQL: NEW.journal_entry_id is a posted entry of the given source with both legs equal to NEW.amount."""
    sources = ", ".join(f"'{source}'" for source in source_types)
    leg = ("EXISTS (SELECT 1 FROM accounting_journalline l JOIN accounting_chartofaccount a"
           " ON a.id = l.account_id WHERE l.entry_id = e.id AND a.code = '{code}' AND l.{side} = NEW.amount)")
    return (f"EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.journal_entry_id"
            f" AND e.status = 'posted' AND e.source_type IN ({sources})"
            f" AND {leg.format(code=debit_code, side='debit')} AND {leg.format(code=credit_code, side='credit')})")


def sum_of(table, column, value):
    return f"(SELECT COALESCE(SUM(amount), 0) FROM {table} WHERE {column} = {value})"


def invoice_open(invoice):
    return (f"{invoice}.posted_total - {sum_of('accounting_paymentallocation', 'invoice_id', f'{invoice}.id')}"
            f" - {sum_of('accounting_creditnote', 'invoice_id', f'{invoice}.id')}")


def payment_unapplied(payment):
    return (f"{payment}.amount - {sum_of('accounting_paymentallocation', 'payment_id', f'{payment}.id')}"
            f" - {sum_of('accounting_paymentrefund', 'payment_id', f'{payment}.id')}")


# Migrated receipt entries keep source_type 'invoice'.
PAYMENT_OK = entry_matches(["customer_payment", "invoice"], "1000", "1100")
REFUND_OK = entry_matches(["payment_refund"], "1100", "1000")
CREDIT_NOTE_OK = entry_matches(["credit_note"], "4000", "1100")
NO_SETTLEMENTS = ("NOT EXISTS (SELECT 1 FROM accounting_paymentallocation WHERE invoice_id = OLD.id)"
                  " AND NOT EXISTS (SELECT 1 FROM accounting_creditnote WHERE invoice_id = OLD.id)")
APPEND_ONLY = ["customerpayment", "paymentallocation", "paymentrefund", "creditnote"]

SQLITE_INVOICE_UPDATE = AR_0005.SQLITE_FORWARD[0].replace(
    "AND NEW.journal_entry_id IS OLD.journal_entry_id",
    f"AND NEW.journal_entry_id IS OLD.journal_entry_id AND {NO_SETTLEMENTS}")
assert SQLITE_INVOICE_UPDATE != AR_0005.SQLITE_FORWARD[0]

SQLITE_FORWARD = [
    "DROP TRIGGER accounting_invoice_guard_update",
    SQLITE_INVOICE_UPDATE,
    f"""
    CREATE TRIGGER accounting_customerpayment_guard_insert BEFORE INSERT ON accounting_customerpayment
    WHEN NOT {PAYMENT_OK}
    BEGIN SELECT RAISE(ABORT, 'Payment requires a posted entry debiting cash and crediting AR.'); END
    """,
    f"""
    CREATE TRIGGER accounting_paymentallocation_guard_insert BEFORE INSERT ON accounting_paymentallocation
    WHEN NOT EXISTS (
        SELECT 1 FROM accounting_customerpayment p JOIN accounting_invoice i ON i.id = NEW.invoice_id
        WHERE p.id = NEW.payment_id AND i.status = 'posted' AND i.customer_id = p.customer_id
        AND i.currency = p.currency AND NEW.allocated_on >= p.received_on AND NEW.allocated_on >= i.issue_date
        AND NEW.amount <= {invoice_open('i')} AND NEW.amount <= {payment_unapplied('p')})
    BEGIN SELECT RAISE(ABORT, 'Allocation requires a matching posted invoice and enough open and unapplied balance.'); END
    """,
    f"""
    CREATE TRIGGER accounting_paymentrefund_guard_insert BEFORE INSERT ON accounting_paymentrefund
    WHEN NOT ({REFUND_OK} AND EXISTS (
        SELECT 1 FROM accounting_customerpayment p WHERE p.id = NEW.payment_id
        AND NEW.refunded_on >= p.received_on AND NEW.amount <= {payment_unapplied('p')}))
    BEGIN SELECT RAISE(ABORT, 'Refund requires its posted entry and enough unapplied payment.'); END
    """,
    f"""
    CREATE TRIGGER accounting_creditnote_guard_insert BEFORE INSERT ON accounting_creditnote
    WHEN NOT ({CREDIT_NOTE_OK} AND EXISTS (
        SELECT 1 FROM accounting_invoice i WHERE i.id = NEW.invoice_id AND i.status = 'posted'
        AND NEW.issued_on >= i.issue_date AND NEW.amount <= {invoice_open('i')}))
    BEGIN SELECT RAISE(ABORT, 'Credit note requires its posted entry and an open invoice balance.'); END
    """,
] + [
    f"""
    CREATE TRIGGER accounting_{table}_guard_{operation} BEFORE {operation.upper()} ON accounting_{table}
    BEGIN SELECT RAISE(ABORT, 'Settlement records are immutable.'); END
    """
    for table in APPEND_ONLY for operation in ("update", "delete")
]

SQLITE_REVERSE = [
    f"DROP TRIGGER IF EXISTS accounting_{table}_guard_{operation}"
    for table in APPEND_ONLY for operation in ("insert", "update", "delete")
] + ["DROP TRIGGER accounting_invoice_guard_update", AR_0005.SQLITE_FORWARD[0]]

POSTGRES_INVOICE_FUNCTION = AR_0005.POSTGRES_FORWARD[0].replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION").replace(
    "AND NEW.journal_entry_id = OLD.journal_entry_id",
    f"AND NEW.journal_entry_id = OLD.journal_entry_id AND {NO_SETTLEMENTS}")
assert NO_SETTLEMENTS in POSTGRES_INVOICE_FUNCTION


def postgres_guard(table, body):
    """Insert-only trigger: `body` validates NEW and may lock parent rows."""
    return [
        f"""
        CREATE FUNCTION accounting_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Settlement records are immutable.' USING ERRCODE = 'check_violation';
            END IF;
            {body}
            RETURN NEW;
        END $$
        """,
        f"""
        CREATE TRIGGER accounting_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_{table}
        FOR EACH ROW EXECUTE FUNCTION accounting_{table}_guard()
        """,
    ]


def postgres_reject(condition, message):
    return f"""
            IF NOT ({condition}) THEN
                RAISE EXCEPTION '{message}' USING ERRCODE = 'check_violation';
            END IF;"""


POSTGRES_FORWARD = [POSTGRES_INVOICE_FUNCTION] + postgres_guard(
    "customerpayment",
    postgres_reject(PAYMENT_OK, "Payment requires a posted entry debiting cash and crediting AR."),
) + postgres_guard(
    "paymentallocation",
    # Lock order matches allocate_payment: payment, then invoice.
    "PERFORM 1 FROM accounting_customerpayment WHERE id = NEW.payment_id FOR UPDATE;"
    " PERFORM 1 FROM accounting_invoice WHERE id = NEW.invoice_id FOR UPDATE;"
    + postgres_reject(
        "EXISTS (SELECT 1 FROM accounting_customerpayment p JOIN accounting_invoice i ON i.id = NEW.invoice_id"
        " WHERE p.id = NEW.payment_id AND i.status = 'posted' AND i.customer_id = p.customer_id"
        " AND i.currency = p.currency AND NEW.allocated_on >= p.received_on"
        f" AND NEW.allocated_on >= i.issue_date AND NEW.amount <= {invoice_open('i')}"
        f" AND NEW.amount <= {payment_unapplied('p')})",
        "Allocation requires a matching posted invoice and enough open and unapplied balance."),
) + postgres_guard(
    "paymentrefund",
    "PERFORM 1 FROM accounting_customerpayment WHERE id = NEW.payment_id FOR UPDATE;"
    + postgres_reject(
        f"{REFUND_OK} AND EXISTS (SELECT 1 FROM accounting_customerpayment p WHERE p.id = NEW.payment_id"
        f" AND NEW.refunded_on >= p.received_on AND NEW.amount <= {payment_unapplied('p')})",
        "Refund requires its posted entry and enough unapplied payment."),
) + postgres_guard(
    "creditnote",
    "PERFORM 1 FROM accounting_invoice WHERE id = NEW.invoice_id FOR UPDATE;"
    + postgres_reject(
        f"{CREDIT_NOTE_OK} AND EXISTS (SELECT 1 FROM accounting_invoice i WHERE i.id = NEW.invoice_id"
        f" AND i.status = 'posted' AND NEW.issued_on >= i.issue_date AND NEW.amount <= {invoice_open('i')})",
        "Credit note requires its posted entry and an open invoice balance."),
)

POSTGRES_REVERSE = [
    statement for table in APPEND_ONLY for statement in (
        f"DROP TRIGGER IF EXISTS accounting_{table}_guard ON accounting_{table}",
        f"DROP FUNCTION IF EXISTS accounting_{table}_guard()")
] + [AR_0005.POSTGRES_FORWARD[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")]

RECEIPT_TRIGGERS = {
    "sqlite": (AR_0005.SQLITE_FORWARD[7:], [
        "DROP TRIGGER IF EXISTS accounting_invoicereceipt_guard_insert",
        "DROP TRIGGER IF EXISTS accounting_invoicereceipt_guard_update",
        "DROP TRIGGER IF EXISTS accounting_invoicereceipt_guard_delete",
    ]),
    "postgresql": (AR_0005.POSTGRES_FORWARD[4:], AR_0005.POSTGRES_REVERSE[:2]),
}
assert all("invoicereceipt" in sql for sql in RECEIPT_TRIGGERS["sqlite"][0] + RECEIPT_TRIGGERS["postgresql"][0])


def execute(statements_by_vendor, index):
    def run(apps, schema_editor):
        vendor = schema_editor.connection.vendor
        if vendor not in statements_by_vendor:
            raise RuntimeError(f"AR settlement triggers are not defined for {vendor}.")
        for statement in statements_by_vendor[vendor][index]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


SETTLEMENT_TRIGGERS = {"sqlite": (SQLITE_FORWARD, SQLITE_REVERSE),
                       "postgresql": (POSTGRES_FORWARD, POSTGRES_REVERSE)}


def receipts_to_payments(apps, schema_editor):
    receipt_model = apps.get_model("accounting", "InvoiceReceipt")
    payment_model = apps.get_model("accounting", "CustomerPayment")
    allocation_model = apps.get_model("accounting", "PaymentAllocation")
    for receipt in receipt_model.objects.select_related("invoice").order_by("received_on", "created_at"):
        payment = payment_model.objects.create(
            customer_id=receipt.invoice.customer_id, reference=receipt.reference, amount=receipt.amount,
            currency=receipt.invoice.currency, received_on=receipt.received_on,
            journal_entry_id=receipt.journal_entry_id)
        allocation_model.objects.create(payment=payment, invoice_id=receipt.invoice_id,
            reference=receipt.reference, amount=receipt.amount, allocated_on=receipt.received_on)


def payments_to_receipts(apps, schema_editor):
    """Reverse only when every payment is a migrated receipt: one full allocation, nothing else."""
    receipt_model = apps.get_model("accounting", "InvoiceReceipt")
    payment_model = apps.get_model("accounting", "CustomerPayment")
    if apps.get_model("accounting", "CreditNote").objects.exists() or \
            apps.get_model("accounting", "PaymentRefund").objects.exists():
        raise RuntimeError("Credit notes or refunds exist; they cannot be represented as receipts.")
    for payment in payment_model.objects.prefetch_related("allocations"):
        allocations = list(payment.allocations.all())
        if len(allocations) != 1 or allocations[0].amount != payment.amount:
            raise RuntimeError(f"Payment {payment.reference} is not a single full allocation.")
        receipt_model.objects.create(invoice_id=allocations[0].invoice_id, reference=payment.reference,
            amount=payment.amount, received_on=payment.received_on, journal_entry_id=payment.journal_entry_id)
    # Settlement triggers were already dropped by the reverse of the last operation.
    apps.get_model("accounting", "PaymentAllocation").objects.all().delete()
    payment_model.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0005_ar_document_integrity"),
    ]

    operations = [
        migrations.CreateModel(
            name="CreditNote",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("number", models.CharField(max_length=40, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("issued_on", models.DateField()),
                ("reason", models.CharField(max_length=500)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name="credit_notes", to="accounting.invoice")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                                       related_name="credit_note", to="accounting.journalentry")),
            ],
        ),
        migrations.CreateModel(
            name="CustomerPayment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reference", models.CharField(max_length=100, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("currency", models.CharField(max_length=3)),
                ("received_on", models.DateField()),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                               related_name="payments", to="accounting.customer")),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                                       related_name="customer_payment", to="accounting.journalentry")),
            ],
        ),
        migrations.CreateModel(
            name="PaymentAllocation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reference", models.CharField(max_length=100, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("allocated_on", models.DateField()),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name="allocations", to="accounting.invoice")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name="allocations", to="accounting.customerpayment")),
            ],
        ),
        migrations.CreateModel(
            name="PaymentRefund",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reference", models.CharField(max_length=100, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18)),
                ("refunded_on", models.DateField()),
                ("journal_entry", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                                       related_name="payment_refund", to="accounting.journalentry")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name="refunds", to="accounting.customerpayment")),
            ],
        ),
        migrations.AddConstraint(
            model_name="paymentrefund",
            constraint=models.CheckConstraint(check=models.Q(("amount__gt", 0)), name="payment_refund_positive"),
        ),
        migrations.AddConstraint(
            model_name="paymentallocation",
            constraint=models.CheckConstraint(check=models.Q(("amount__gt", 0)), name="payment_allocation_positive"),
        ),
        migrations.AddConstraint(
            model_name="customerpayment",
            constraint=models.CheckConstraint(check=models.Q(("amount__gt", 0)), name="customer_payment_positive"),
        ),
        migrations.AddConstraint(
            model_name="customerpayment",
            constraint=models.CheckConstraint(check=models.Q(("currency__in", ("USD", "EUR", "GBP"))),
                                              name="customer_payment_currency_supported"),
        ),
        migrations.AddConstraint(
            model_name="creditnote",
            constraint=models.CheckConstraint(check=models.Q(("amount__gt", 0)), name="credit_note_positive"),
        ),
        migrations.RunPython(receipts_to_payments, payments_to_receipts),
        migrations.RunPython(execute(RECEIPT_TRIGGERS, 1), execute(RECEIPT_TRIGGERS, 0)),
        migrations.DeleteModel(name="InvoiceReceipt"),
        migrations.RunPython(execute(SETTLEMENT_TRIGGERS, 0), execute(SETTLEMENT_TRIGGERS, 1)),
    ]
