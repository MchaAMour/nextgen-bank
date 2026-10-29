"""Database enforcement for the general ledger (INT-01 to INT-04, REV-01).

- Journal entries are inserted as drafts. A draft becomes posted only with at least
  two lines whose debits equal their credits. A posted entry changes only by being
  marked reversed, once a posted entry reversing it exists. Only drafts can be
  deleted. A user who posted entries therefore cannot be deleted; deactivate instead.
- Lines can be inserted, changed or deleted only while their entries are drafts;
  PostgreSQL locks the entry rows first, like the posting service.
- Every line has exactly one positive side (checked against existing rows first).
- An account's code and type cannot change once it has lines in non-draft entries.

SQLite rebuilds the line table to add the check constraint, so all accounting
triggers are suspended around it as in 0008.
"""

import importlib

from django.db import migrations, models

EUR_0008 = importlib.import_module("core_apps.accounting.migrations.0008_eur_base_currency")


def lines_of(entry):
    return f"FROM accounting_journalline WHERE entry_id = {entry}"


BALANCED = (f"(SELECT COUNT(*) {lines_of('NEW.id')}) >= 2"
            f" AND ROUND((SELECT SUM(debit) {lines_of('NEW.id')}) - (SELECT SUM(credit) {lines_of('NEW.id')}), 2) = 0")
ENTRY_COLUMNS = ["id", "entry_date", "memo", "reference", "source_type", "source_id", "posted_at", "posted_by_id",
                 "reversal_of_id", "created_at"]


def entry_update_ok(equal):
    same = " AND ".join(f"NEW.{column} {equal} OLD.{column}" for column in ENTRY_COLUMNS)
    return f"""(
        (OLD.status = 'draft' AND NEW.status = 'draft')
        OR (OLD.status = 'draft' AND NEW.status = 'posted' AND NEW.posted_at IS NOT NULL AND {BALANCED})
        OR (OLD.status = 'posted' AND NEW.status = 'reversed' AND {same}
            AND EXISTS (SELECT 1 FROM accounting_journalentry r WHERE r.reversal_of_id = OLD.id
                        AND r.status IN ('posted', 'reversed'))))"""


ACCOUNT_LOCKED = """(NEW.code <> OLD.code OR NEW.account_type <> OLD.account_type
    OR NEW.normal_balance <> OLD.normal_balance) AND EXISTS (
    SELECT 1 FROM accounting_journalline l JOIN accounting_journalentry e ON e.id = l.entry_id
    WHERE l.account_id = OLD.id AND e.status <> 'draft')"""
MESSAGES = {
    "insert": "Journal entries must be created as drafts.",
    "update": "Journal entries change only by posting a balanced draft or by reversal.",
    "delete": "Only draft journal entries can be deleted.",
    "line": "Lines of posted journal entries are immutable.",
    "account": "The code and type of an account with posted lines cannot change.",
}

SQLITE_FORWARD = [
    f"""
    CREATE TRIGGER accounting_journalentry_guard_insert BEFORE INSERT ON accounting_journalentry
    WHEN NEW.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["insert"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_journalentry_guard_update BEFORE UPDATE ON accounting_journalentry
    WHEN NOT {entry_update_ok("IS")}
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["update"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_journalentry_guard_delete BEFORE DELETE ON accounting_journalentry
    WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["delete"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_journalline_guard_insert BEFORE INSERT ON accounting_journalline
    WHEN (SELECT status FROM accounting_journalentry WHERE id = NEW.entry_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["line"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_journalline_guard_update BEFORE UPDATE ON accounting_journalline
    WHEN EXISTS (SELECT 1 FROM accounting_journalentry WHERE id IN (OLD.entry_id, NEW.entry_id)
                 AND status <> 'draft')
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["line"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_journalline_guard_delete BEFORE DELETE ON accounting_journalline
    WHEN (SELECT status FROM accounting_journalentry WHERE id = OLD.entry_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["line"]}'); END
    """,
    f"""
    CREATE TRIGGER accounting_chartofaccount_guard_update BEFORE UPDATE ON accounting_chartofaccount
    WHEN {ACCOUNT_LOCKED}
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["account"]}'); END
    """,
]
SQLITE_REVERSE = [f"DROP TRIGGER IF EXISTS accounting_{name}" for name in (
    "journalentry_guard_insert", "journalentry_guard_update", "journalentry_guard_delete",
    "journalline_guard_insert", "journalline_guard_update", "journalline_guard_delete",
    "chartofaccount_guard_update")]


def postgres_raise(key):
    return f"RAISE EXCEPTION '{MESSAGES[key]}' USING ERRCODE = 'check_violation';"


POSTGRES_FORWARD = [
    f"""
    CREATE FUNCTION accounting_journalentry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'draft' THEN {postgres_raise("insert")} END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            IF OLD.status <> 'draft' THEN {postgres_raise("delete")} END IF;
            RETURN OLD;
        END IF;
        IF NOT {entry_update_ok("IS NOT DISTINCT FROM")} THEN {postgres_raise("update")} END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_journalentry_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_journalentry
    FOR EACH ROW EXECUTE FUNCTION accounting_journalentry_guard()
    """,
    f"""
    CREATE FUNCTION accounting_journalline_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        parent_status text;
    BEGIN
        -- FOR SHARE waits for a concurrent posting's FOR UPDATE lock, then sees the committed status.
        FOR parent_status IN
            SELECT status FROM accounting_journalentry
            WHERE id IN (CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.entry_id END,
                         CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE NEW.entry_id END)
            ORDER BY id FOR SHARE
        LOOP
            IF parent_status <> 'draft' THEN {postgres_raise("line")} END IF;
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_journalline_guard BEFORE INSERT OR UPDATE OR DELETE ON accounting_journalline
    FOR EACH ROW EXECUTE FUNCTION accounting_journalline_guard()
    """,
    f"""
    CREATE FUNCTION accounting_chartofaccount_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF {ACCOUNT_LOCKED} THEN {postgres_raise("account")} END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_chartofaccount_guard BEFORE UPDATE ON accounting_chartofaccount
    FOR EACH ROW EXECUTE FUNCTION accounting_chartofaccount_guard()
    """,
]
POSTGRES_REVERSE = [statement for table in ("journalentry", "journalline", "chartofaccount") for statement in (
    f"DROP TRIGGER IF EXISTS accounting_{table}_guard ON accounting_{table}",
    f"DROP FUNCTION IF EXISTS accounting_{table}_guard()")]
STATEMENTS = {"sqlite": (SQLITE_FORWARD, SQLITE_REVERSE), "postgresql": (POSTGRES_FORWARD, POSTGRES_REVERSE)}


def execute(direction):
    def run(apps, schema_editor):
        for statement in STATEMENTS[schema_editor.connection.vendor][direction]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


def require_valid_ledger(apps, schema_editor):
    """Refuse to migrate over rows the new rules would reject, rather than failing midway."""
    checks = {
        "lines without exactly one positive side": "SELECT COUNT(*) FROM accounting_journalline"
            " WHERE NOT ((debit > 0 AND credit = 0) OR (debit = 0 AND credit > 0))",
        "posted entries that do not balance": "SELECT COUNT(*) FROM accounting_journalentry e WHERE e.status <> 'draft'"
            " AND ((SELECT COUNT(*) FROM accounting_journalline WHERE entry_id = e.id) < 2"
            " OR ROUND((SELECT SUM(debit) - SUM(credit) FROM accounting_journalline WHERE entry_id = e.id), 2) <> 0)",
    }
    found = {}
    with schema_editor.connection.cursor() as cursor:
        for label, sql in checks.items():
            cursor.execute(sql)
            count = cursor.fetchone()[0]
            if count:
                found[label] = count
    if found:
        raise RuntimeError(f"The ledger has rows the integrity rules reject: {found}. Correct them and migrate again.")


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0012_accounts_payable'),
    ]

    operations = [
        migrations.RunPython(require_valid_ledger, migrations.RunPython.noop),
        migrations.RunPython(EUR_0008.suspend_sqlite_triggers, EUR_0008.restore_sqlite_triggers),
        migrations.AddConstraint(
            model_name='journalline',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('credit', 0), ('debit__gt', 0)), models.Q(('credit__gt', 0), ('debit', 0)), _connector='OR'), name='accounting_line_one_side'),
        ),
        migrations.RunPython(EUR_0008.restore_sqlite_triggers, EUR_0008.suspend_sqlite_triggers),
        migrations.RunPython(execute(0), execute(1)),
    ]
