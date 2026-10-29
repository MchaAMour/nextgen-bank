"""Allocation releases (AR-05): a payment allocation can be taken back in full.

Releases are insert-only and have no ledger effect. Balance checks in the 0006
settlement triggers (and the 0009 refund trigger) count only allocations that are
not released, and a posted invoice whose allocations are all released can be
cancelled again. Those triggers are rewritten from their current definitions in
the database, so the rewrite keeps every later change to them.
"""

import re

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

NOT_RELEASED = "NOT EXISTS (SELECT 1 FROM accounting_allocationrelease ar WHERE ar.allocation_id = pa.id)"
# (pattern in 0006 SQL, its release-aware form), applied forward; the reverse swaps them.
REWRITES = [
    (r"(SELECT COALESCE(SUM(amount), 0) FROM accounting_paymentallocation WHERE {col} = {val})",
     r"(SELECT COALESCE(SUM(pa.amount), 0) FROM accounting_paymentallocation pa WHERE pa.{col} = {val}"
     f" AND {NOT_RELEASED})"),
    (r"NOT EXISTS (SELECT 1 FROM accounting_paymentallocation WHERE invoice_id = OLD.id)",
     f"NOT EXISTS (SELECT 1 FROM accounting_paymentallocation pa WHERE pa.invoice_id = OLD.id AND {NOT_RELEASED})"),
]
SQLITE_TRIGGERS = ["accounting_invoice_guard_update", "accounting_paymentallocation_guard_insert",
                   "accounting_paymentrefund_guard_insert", "accounting_creditnote_guard_insert"]
POSTGRES_FUNCTIONS = ["accounting_invoice_guard", "accounting_paymentallocation_guard",
                      "accounting_paymentrefund_guard", "accounting_creditnote_guard"]


def pattern(template):
    regex = re.escape(template).replace(re.escape("{col}"), r"(?P<col>\w+)").replace(
        re.escape("{val}"), r"(?P<val>[\w.]+)")
    replacement = template.replace("{col}", r"\g<col>").replace("{val}", r"\g<val>")
    return re.compile(regex), replacement


def rewrite(sql, forward):
    changed = 0
    for old, new in REWRITES:
        source, target = (old, new) if forward else (new, old)
        regex, _ = pattern(source)
        _, replacement = pattern(target)
        sql, count = regex.subn(replacement, sql)
        changed += count
    if not changed:
        raise RuntimeError("Allocation balance SQL not found in a settlement trigger.")
    return sql


def rewrite_triggers(forward):
    def run(apps, schema_editor):
        vendor = schema_editor.connection.vendor
        with schema_editor.connection.cursor() as cursor:
            if vendor == "sqlite":
                definitions = []
                for name in SQLITE_TRIGGERS:
                    cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = %s", [name])
                    definitions.append((name, cursor.fetchone()[0]))
            elif vendor == "postgresql":
                definitions = []
                for name in POSTGRES_FUNCTIONS:
                    cursor.execute("SELECT pg_get_functiondef(%s::regproc)", [name])
                    definitions.append((name, cursor.fetchone()[0]))
            else:
                raise RuntimeError(f"Allocation release triggers are not defined for {vendor}.")
        for name, sql in definitions:
            if vendor == "sqlite":
                schema_editor.execute(f"DROP TRIGGER {name}", params=None)
            # pg_get_functiondef returns CREATE OR REPLACE FUNCTION; triggers keep using it.
            schema_editor.execute(rewrite(sql, forward), params=None)
        execute(RELEASE_TRIGGERS[vendor][0 if forward else 1], schema_editor)

    return run


def execute(statements, schema_editor):
    for statement in statements:
        # params=None sends PL/pgSQL without placeholder interpolation.
        schema_editor.execute(statement, params=None)


RELEASE_OK = ("EXISTS (SELECT 1 FROM accounting_paymentallocation pa WHERE pa.id = NEW.allocation_id"
              " AND NEW.released_on >= pa.allocated_on)"
              " AND NOT EXISTS (SELECT 1 FROM accounting_periodclose WHERE closed_through >= NEW.released_on)")
MESSAGES = {"insert": "A release must follow its allocation and fall in an open period.",
            "immutable": "Settlement records are immutable."}
RELEASE_TRIGGERS = {
    "sqlite": ([
        f"""
        CREATE TRIGGER accounting_allocationrelease_guard_insert BEFORE INSERT ON accounting_allocationrelease
        WHEN NOT ({RELEASE_OK})
        BEGIN SELECT RAISE(ABORT, '{MESSAGES["insert"]}'); END
        """,
    ] + [
        f"""
        CREATE TRIGGER accounting_allocationrelease_guard_{operation} BEFORE {operation.upper()}
        ON accounting_allocationrelease
        BEGIN SELECT RAISE(ABORT, '{MESSAGES["immutable"]}'); END
        """ for operation in ("update", "delete")
    ], [f"DROP TRIGGER IF EXISTS accounting_allocationrelease_guard_{operation}"
        for operation in ("insert", "update", "delete")]),
    "postgresql": ([
        f"""
        CREATE FUNCTION accounting_allocationrelease_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION '{MESSAGES["immutable"]}' USING ERRCODE = 'check_violation';
            END IF;
            -- Lock order matches allocate_payment: payment, then invoice.
            PERFORM 1 FROM accounting_customerpayment p JOIN accounting_paymentallocation pa
                ON pa.payment_id = p.id WHERE pa.id = NEW.allocation_id FOR UPDATE OF p;
            PERFORM 1 FROM accounting_invoice i JOIN accounting_paymentallocation pa
                ON pa.invoice_id = i.id WHERE pa.id = NEW.allocation_id FOR UPDATE OF i;
            IF NOT ({RELEASE_OK}) THEN
                RAISE EXCEPTION '{MESSAGES["insert"]}' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        """
        CREATE TRIGGER accounting_allocationrelease_guard BEFORE INSERT OR UPDATE OR DELETE
        ON accounting_allocationrelease FOR EACH ROW EXECUTE FUNCTION accounting_allocationrelease_guard()
        """,
    ], [
        "DROP TRIGGER IF EXISTS accounting_allocationrelease_guard ON accounting_allocationrelease",
        "DROP FUNCTION IF EXISTS accounting_allocationrelease_guard()",
    ]),
}


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounting', '0014_period_close'),
    ]

    operations = [
        migrations.CreateModel(
            name='AllocationRelease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('released_on', models.DateField()),
                ('reason', models.CharField(max_length=500)),
                ('allocation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='release', to='accounting.paymentallocation')),
                ('released_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='allocation_releases', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.RunPython(rewrite_triggers(True), rewrite_triggers(False)),
    ]
