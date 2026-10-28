"""Database enforcement for AR documents (AR-03).

Check constraints keep each invoice row's lifecycle fields consistent. Triggers
reject writes that bypass the services: raw SQL or private ORM updates of posted
invoices, edits to lines of non-draft invoices, and receipt changes. PostgreSQL
line and receipt triggers lock the parent invoice row, so they serialize with
the posting service's SELECT ... FOR UPDATE.
"""

from django.db import migrations, models

SQLITE_FORWARD = [
    """
    CREATE TRIGGER accounting_invoice_guard_update BEFORE UPDATE ON accounting_invoice
    WHEN OLD.status <> 'draft' AND NOT (
        OLD.status = 'posted' AND NEW.status = 'cancelled'
        AND NEW.id IS OLD.id AND NEW.number IS OLD.number AND NEW.customer_id IS OLD.customer_id
        AND NEW.issue_date IS OLD.issue_date AND NEW.due_date IS OLD.due_date
        AND NEW.currency IS OLD.currency AND NEW.posted_total IS OLD.posted_total
        AND NEW.journal_entry_id IS OLD.journal_entry_id AND NEW.created_at IS OLD.created_at
        AND EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.cancellation_entry_id
                    AND e.status = 'posted' AND e.reversal_of_id = OLD.journal_entry_id))
    BEGIN SELECT RAISE(ABORT, 'Posted or cancelled invoices are immutable.'); END
    """,
    """
    CREATE TRIGGER accounting_invoice_guard_post BEFORE UPDATE ON accounting_invoice
    WHEN OLD.status = 'draft' AND NEW.status = 'posted' AND NOT EXISTS (
        SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.journal_entry_id
        AND e.status = 'posted' AND e.source_type = 'invoice' AND e.source_id = NEW.id)
    BEGIN SELECT RAISE(ABORT, 'Invoice posting requires its posted journal entry.'); END
    """,
    """
    CREATE TRIGGER accounting_invoice_guard_insert BEFORE INSERT ON accounting_invoice
    WHEN NEW.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Invoices must be created as drafts.'); END
    """,
    """
    CREATE TRIGGER accounting_invoice_guard_delete BEFORE DELETE ON accounting_invoice
    WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Posted or cancelled invoices cannot be deleted.'); END
    """,
    """
    CREATE TRIGGER accounting_invoiceline_guard_insert BEFORE INSERT ON accounting_invoiceline
    WHEN (SELECT status FROM accounting_invoice WHERE id = NEW.invoice_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Lines of non-draft invoices are immutable.'); END
    """,
    """
    CREATE TRIGGER accounting_invoiceline_guard_update BEFORE UPDATE ON accounting_invoiceline
    WHEN EXISTS (SELECT 1 FROM accounting_invoice WHERE id IN (OLD.invoice_id, NEW.invoice_id)
                 AND status <> 'draft')
    BEGIN SELECT RAISE(ABORT, 'Lines of non-draft invoices are immutable.'); END
    """,
    """
    CREATE TRIGGER accounting_invoiceline_guard_delete BEFORE DELETE ON accounting_invoiceline
    WHEN (SELECT status FROM accounting_invoice WHERE id = OLD.invoice_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'Lines of non-draft invoices are immutable.'); END
    """,
    """
    CREATE TRIGGER accounting_invoicereceipt_guard_insert BEFORE INSERT ON accounting_invoicereceipt
    WHEN NOT EXISTS (
        SELECT 1 FROM accounting_invoice i JOIN accounting_journalentry e ON e.id = NEW.journal_entry_id
        WHERE i.id = NEW.invoice_id AND i.status = 'posted' AND NEW.received_on >= i.issue_date
        AND e.status = 'posted' AND e.source_type = 'invoice' AND e.source_id = i.id
        AND EXISTS (SELECT 1 FROM accounting_journalline l JOIN accounting_chartofaccount a ON a.id = l.account_id
                    WHERE l.entry_id = e.id AND a.code = '1100' AND l.credit = NEW.amount)
        AND NEW.amount <= i.posted_total - (SELECT COALESCE(SUM(r.amount), 0)
                                            FROM accounting_invoicereceipt r WHERE r.invoice_id = i.id))
    BEGIN SELECT RAISE(ABORT, 'Receipt requires a posted invoice, its journal entry and an open balance.'); END
    """,
    """
    CREATE TRIGGER accounting_invoicereceipt_guard_update BEFORE UPDATE ON accounting_invoicereceipt
    BEGIN SELECT RAISE(ABORT, 'Receipts are immutable.'); END
    """,
    """
    CREATE TRIGGER accounting_invoicereceipt_guard_delete BEFORE DELETE ON accounting_invoicereceipt
    BEGIN SELECT RAISE(ABORT, 'Receipts cannot be deleted.'); END
    """,
]

SQLITE_REVERSE = [
    f"DROP TRIGGER IF EXISTS {name}"
    for name in [
        "accounting_invoice_guard_update", "accounting_invoice_guard_post",
        "accounting_invoice_guard_insert", "accounting_invoice_guard_delete",
        "accounting_invoiceline_guard_insert", "accounting_invoiceline_guard_update",
        "accounting_invoiceline_guard_delete", "accounting_invoicereceipt_guard_insert",
        "accounting_invoicereceipt_guard_update", "accounting_invoicereceipt_guard_delete",
    ]
]

# check_violation (23514) surfaces as django.db.IntegrityError.
POSTGRES_FORWARD = [
    """
    CREATE FUNCTION accounting_invoice_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'draft' THEN
                RAISE EXCEPTION 'Invoices must be created as drafts.' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            IF OLD.status <> 'draft' THEN
                RAISE EXCEPTION 'Posted or cancelled invoices cannot be deleted.'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN OLD;
        END IF;
        IF OLD.status = 'draft' AND NEW.status = 'posted' AND NOT EXISTS (
            SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.journal_entry_id
            AND e.status = 'posted' AND e.source_type = 'invoice' AND e.source_id = NEW.id) THEN
            RAISE EXCEPTION 'Invoice posting requires its posted journal entry.'
                USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.status <> 'draft' AND NOT (
            OLD.status = 'posted' AND NEW.status = 'cancelled'
            AND NEW.id = OLD.id AND NEW.number = OLD.number AND NEW.customer_id = OLD.customer_id
            AND NEW.issue_date = OLD.issue_date AND NEW.due_date = OLD.due_date
            AND NEW.currency = OLD.currency AND NEW.posted_total = OLD.posted_total
            AND NEW.journal_entry_id = OLD.journal_entry_id AND NEW.created_at = OLD.created_at
            AND EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.cancellation_entry_id
                        AND e.status = 'posted' AND e.reversal_of_id = OLD.journal_entry_id)) THEN
            RAISE EXCEPTION 'Posted or cancelled invoices are immutable.' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_invoice_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_invoice
    FOR EACH ROW EXECUTE FUNCTION accounting_invoice_guard()
    """,
    """
    CREATE FUNCTION accounting_invoiceline_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        parent_status text;
    BEGIN
        -- Lock every parent row before reading its status: FOR SHARE waits for a
        -- concurrent posting's FOR UPDATE lock and then sees the committed status.
        FOR parent_status IN
            SELECT status FROM accounting_invoice
            WHERE id IN (CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.invoice_id END,
                         CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.invoice_id END)
            ORDER BY id FOR SHARE
        LOOP
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION 'Lines of non-draft invoices are immutable.'
                    USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_invoiceline_guard BEFORE INSERT OR UPDATE OR DELETE
    ON accounting_invoiceline FOR EACH ROW EXECUTE FUNCTION accounting_invoiceline_guard()
    """,
    """
    CREATE FUNCTION accounting_invoicereceipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        parent accounting_invoice%ROWTYPE;
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Receipts are immutable.' USING ERRCODE = 'check_violation';
        END IF;
        -- FOR UPDATE serializes receipt inserts, so concurrent receipts cannot overpay.
        SELECT * INTO parent FROM accounting_invoice WHERE id = NEW.invoice_id FOR UPDATE;
        IF parent.status IS DISTINCT FROM 'posted' OR NEW.received_on < parent.issue_date
           OR NOT EXISTS (SELECT 1 FROM accounting_journalentry e WHERE e.id = NEW.journal_entry_id
                          AND e.status = 'posted' AND e.source_type = 'invoice' AND e.source_id = parent.id
                          AND EXISTS (SELECT 1 FROM accounting_journalline l
                                      JOIN accounting_chartofaccount a ON a.id = l.account_id
                                      WHERE l.entry_id = e.id AND a.code = '1100' AND l.credit = NEW.amount))
           OR NEW.amount > parent.posted_total - (SELECT COALESCE(SUM(r.amount), 0)
                                                  FROM accounting_invoicereceipt r
                                                  WHERE r.invoice_id = parent.id) THEN
            RAISE EXCEPTION 'Receipt requires a posted invoice, its journal entry and an open balance.'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_invoicereceipt_guard BEFORE INSERT OR UPDATE OR DELETE
    ON accounting_invoicereceipt FOR EACH ROW EXECUTE FUNCTION accounting_invoicereceipt_guard()
    """,
]

POSTGRES_REVERSE = [
    "DROP TRIGGER IF EXISTS accounting_invoicereceipt_guard ON accounting_invoicereceipt",
    "DROP FUNCTION IF EXISTS accounting_invoicereceipt_guard()",
    "DROP TRIGGER IF EXISTS accounting_invoiceline_guard ON accounting_invoiceline",
    "DROP FUNCTION IF EXISTS accounting_invoiceline_guard()",
    "DROP TRIGGER IF EXISTS accounting_invoice_guard ON accounting_invoice",
    "DROP FUNCTION IF EXISTS accounting_invoice_guard()",
]

STATEMENTS = {
    "sqlite": (SQLITE_FORWARD, SQLITE_REVERSE),
    "postgresql": (POSTGRES_FORWARD, POSTGRES_REVERSE),
}


def run_statements(index):
    def run(apps, schema_editor):
        vendor = schema_editor.connection.vendor
        if vendor not in STATEMENTS:
            raise RuntimeError(f"AR integrity triggers are not defined for {vendor}.")
        for statement in STATEMENTS[vendor][index]:
            # params=None sends PL/pgSQL such as %ROWTYPE without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0004_invoice_cancellation_entry_invoice_journal_entry_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="invoice",
            options={
                "permissions": [
                    ("post_invoice", "Can post invoices"),
                    ("cancel_invoice", "Can cancel invoices"),
                ]
            },
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(("due_date__gte", models.F("issue_date"))),
                name="invoice_due_not_before_issue",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(("currency__in", ("USD", "EUR", "GBP"))),
                name="invoice_currency_supported",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(
                    models.Q(
                        ("cancellation_entry__isnull", True),
                        ("journal_entry__isnull", True),
                        ("posted_total__isnull", True),
                        ("status", "draft"),
                    ),
                    models.Q(
                        ("cancellation_entry__isnull", True),
                        ("journal_entry__isnull", False),
                        ("posted_total__gt", 0),
                        ("status", "posted"),
                    ),
                    models.Q(
                        ("cancellation_entry__isnull", True),
                        ("journal_entry__isnull", True),
                        ("posted_total__isnull", True),
                        ("status", "cancelled"),
                    ),
                    models.Q(
                        ("cancellation_entry__isnull", False),
                        ("journal_entry__isnull", False),
                        ("posted_total__gt", 0),
                        ("status", "cancelled"),
                    ),
                    _connector="OR",
                ),
                name="invoice_lifecycle_consistent",
            ),
        ),
        migrations.RunPython(run_statements(0), run_statements(1)),
    ]
