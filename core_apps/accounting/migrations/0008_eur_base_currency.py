"""EUR becomes the single ledger currency (MONEY-01 decision, 2026-10-28).

The Stripe account settles in EUR, so invoices, payments and journal lines are
restricted to EUR until FX accounting exists. The migration refuses to run while
non-EUR rows exist, rather than failing on a constraint.

On SQLite, changing a table's constraints rebuilds the table: its own triggers
are dropped, and renaming the rebuilt table fails while triggers on other tables
reference it. All accounting triggers are therefore saved and dropped first and
recreated afterwards. PostgreSQL alters the tables in place and keeps them.
"""

from django.db import migrations, models

CURRENCY_TABLES = ["accounting_invoice", "accounting_customerpayment", "accounting_journalline",
                   "accounting_stripecheckoutsession"]
saved_triggers = []


def require_eur_only(apps, schema_editor):
    found = {}
    with schema_editor.connection.cursor() as cursor:
        for table in CURRENCY_TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE currency <> 'EUR'")
            count = cursor.fetchone()[0]
            if count:
                found[table] = count
    if found:
        raise RuntimeError(f"Non-EUR rows exist: {found}. EUR is now the only ledger currency; convert or "
                           "remove them (for a disposable local database, recreate it) and migrate again.")


def suspend_sqlite_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'accounting_%'")
        rows = cursor.fetchall()
    saved_triggers[:] = [sql for _, sql in rows]
    for name, _ in rows:
        schema_editor.execute(f"DROP TRIGGER {name}", params=None)


def restore_sqlite_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    for sql in saved_triggers:
        schema_editor.execute(sql, params=None)


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0007_stripe_checkout"),
    ]

    operations = [
        migrations.RunPython(require_eur_only, migrations.RunPython.noop),
        migrations.RunPython(suspend_sqlite_triggers, restore_sqlite_triggers),
        migrations.RemoveConstraint(
            model_name="customerpayment",
            name="customer_payment_currency_supported",
        ),
        migrations.RemoveConstraint(
            model_name="invoice",
            name="invoice_currency_supported",
        ),
        migrations.AlterField(
            model_name="invoice",
            name="currency",
            field=models.CharField(default="EUR", max_length=3),
        ),
        migrations.AlterField(
            model_name="journalline",
            name="currency",
            field=models.CharField(default="EUR", max_length=3),
        ),
        migrations.AddConstraint(
            model_name="customerpayment",
            constraint=models.CheckConstraint(check=models.Q(("currency__in", ("EUR",))),
                                              name="customer_payment_currency_supported"),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(check=models.Q(("currency__in", ("EUR",))),
                                              name="invoice_currency_supported"),
        ),
        migrations.AddConstraint(
            model_name="journalline",
            constraint=models.CheckConstraint(check=models.Q(("currency", "EUR")),
                                              name="accounting_line_base_currency"),
        ),
        migrations.RunPython(restore_sqlite_triggers, suspend_sqlite_triggers),
    ]
