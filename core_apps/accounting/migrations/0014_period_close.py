"""Period closing (AR-05): nothing may be posted on or before the latest closed date.

Period closes are insert-only. The posting check is a separate trigger next to the
ledger guard from 0013.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

IN_CLOSED_PERIOD = ("NEW.entry_date <= (SELECT MAX(closed_through) FROM accounting_periodclose)")
MESSAGES = {"closed": "The books are closed for this date; post in an open period.",
            "immutable": "Period closes are final."}

SQLITE_FORWARD = [
    f"""
    CREATE TRIGGER accounting_journalentry_guard_period BEFORE UPDATE ON accounting_journalentry
    WHEN OLD.status = 'draft' AND NEW.status = 'posted' AND {IN_CLOSED_PERIOD}
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["closed"]}'); END
    """,
] + [
    f"""
    CREATE TRIGGER accounting_periodclose_guard_{operation} BEFORE {operation.upper()} ON accounting_periodclose
    BEGIN SELECT RAISE(ABORT, '{MESSAGES["immutable"]}'); END
    """ for operation in ("update", "delete")
]
SQLITE_REVERSE = ["DROP TRIGGER IF EXISTS accounting_journalentry_guard_period"] + [
    f"DROP TRIGGER IF EXISTS accounting_periodclose_guard_{operation}" for operation in ("update", "delete")]

POSTGRES_FORWARD = [
    f"""
    CREATE FUNCTION accounting_journalentry_period_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status = 'draft' AND NEW.status = 'posted' AND {IN_CLOSED_PERIOD} THEN
            RAISE EXCEPTION '{MESSAGES["closed"]}' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER accounting_journalentry_period_guard BEFORE UPDATE ON accounting_journalentry
    FOR EACH ROW EXECUTE FUNCTION accounting_journalentry_period_guard()
    """,
    f"""
    CREATE FUNCTION accounting_periodclose_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '{MESSAGES["immutable"]}' USING ERRCODE = 'check_violation';
    END $$
    """,
    """
    CREATE TRIGGER accounting_periodclose_guard BEFORE UPDATE OR DELETE ON accounting_periodclose
    FOR EACH ROW EXECUTE FUNCTION accounting_periodclose_guard()
    """,
]
POSTGRES_REVERSE = [
    "DROP TRIGGER IF EXISTS accounting_journalentry_period_guard ON accounting_journalentry",
    "DROP FUNCTION IF EXISTS accounting_journalentry_period_guard()",
    "DROP TRIGGER IF EXISTS accounting_periodclose_guard ON accounting_periodclose",
    "DROP FUNCTION IF EXISTS accounting_periodclose_guard()",
]
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
        ('accounting', '0013_ledger_integrity'),
    ]

    operations = [
        migrations.CreateModel(
            name='PeriodClose',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('closed_through', models.DateField(unique=True)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='period_closes', to=settings.AUTH_USER_MODEL)),
                ('closing_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='period_close', to='accounting.journalentry')),
            ],
            options={
                'ordering': ['-closed_through'],
            },
        ),
        migrations.RunPython(execute(0), execute(1)),
    ]
