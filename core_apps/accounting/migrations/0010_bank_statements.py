"""Bank statements for cash and bank (1000) and their matches to ledger lines (Goal 6).

Statements and their lines are insert-only. A match may only pair a statement line
with a posted ledger line of the statement's bank account that moves it by the
same signed amount; matches cannot be changed, only deleted (unmatched).
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

MATCH_OK = """EXISTS (
    SELECT 1 FROM accounting_bankstatementline bl
    JOIN accounting_bankstatement s ON s.id = bl.statement_id
    JOIN accounting_journalline l ON l.id = NEW.journal_line_id
    JOIN accounting_journalentry e ON e.id = l.entry_id
    WHERE bl.id = NEW.statement_line_id AND l.account_id = s.account_id AND e.status = 'posted'
    AND l.debit - l.credit = bl.amount)"""
MATCH_MESSAGE = "A bank match needs a posted ledger line of the same bank account and amount."
IMMUTABLE = {"bankstatement": "Bank statements are immutable.", "bankstatementline": "Bank statements are immutable.",
             "bankmatch": "Bank matches cannot be changed; unmatch and match again."}

SQLITE_FORWARD = [
    f"""
    CREATE TRIGGER accounting_bankmatch_guard_insert BEFORE INSERT ON accounting_bankmatch
    WHEN NOT {MATCH_OK}
    BEGIN SELECT RAISE(ABORT, '{MATCH_MESSAGE}'); END
    """,
] + [
    f"""
    CREATE TRIGGER accounting_{table}_guard_{operation} BEFORE {operation.upper()} ON accounting_{table}
    BEGIN SELECT RAISE(ABORT, '{message}'); END
    """
    for table, message in IMMUTABLE.items() for operation in ("update", "delete") if (table, operation) != ("bankmatch", "delete")
]
SQLITE_REVERSE = [f"DROP TRIGGER IF EXISTS accounting_{table}_guard_{operation}"
                  for table in IMMUTABLE for operation in ("insert", "update", "delete")]


def postgres_trigger(table, operations, body):
    return [
        f"""
        CREATE FUNCTION accounting_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            {body}
        END $$
        """,
        f"""
        CREATE TRIGGER accounting_{table}_guard BEFORE {operations} ON accounting_{table}
        FOR EACH ROW EXECUTE FUNCTION accounting_{table}_guard()
        """,
    ]


def postgres_raise(message):
    return f"RAISE EXCEPTION '{message}' USING ERRCODE = 'check_violation';"


POSTGRES_FORWARD = postgres_trigger("bankmatch", "INSERT OR UPDATE", f"""
            IF TG_OP <> 'INSERT' THEN {postgres_raise(IMMUTABLE["bankmatch"])} END IF;
            IF NOT {MATCH_OK} THEN {postgres_raise(MATCH_MESSAGE)} END IF;
            RETURN NEW;""") + [
    statement for table in ("bankstatement", "bankstatementline")
    for statement in postgres_trigger(table, "UPDATE OR DELETE", postgres_raise(IMMUTABLE[table]))
]
POSTGRES_REVERSE = [statement for table in IMMUTABLE for statement in (
    f"DROP TRIGGER IF EXISTS accounting_{table}_guard ON accounting_{table}",
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
        ('accounting', '0009_stripe_balance_transactions'),
    ]

    operations = [
        migrations.CreateModel(
            name='BankStatement',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reference', models.CharField(max_length=100, unique=True)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('opening_balance', models.DecimalField(decimal_places=2, max_digits=18)),
                ('closing_balance', models.DecimalField(decimal_places=2, max_digits=18)),
                ('currency', models.CharField(default='EUR', max_length=3)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bank_statements', to='accounting.chartofaccount')),
                ('imported_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bank_statements', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='BankStatementLine',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('sequence', models.PositiveIntegerField()),
                ('booked_on', models.DateField()),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('description', models.CharField(blank=True, max_length=500)),
                ('bank_reference', models.CharField(blank=True, max_length=255)),
                ('statement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='lines', to='accounting.bankstatement')),
            ],
            options={
                'ordering': ['booked_on', 'sequence'],
            },
        ),
        migrations.CreateModel(
            name='BankMatch',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('method', models.CharField(choices=[('automatic', 'Automatic'), ('manual', 'Manual'), ('posted', 'Posted from the statement')], max_length=10)),
                ('journal_line', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='bank_match', to='accounting.journalline')),
                ('matched_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bank_matches', to=settings.AUTH_USER_MODEL)),
                ('statement_line', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='match', to='accounting.bankstatementline')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddConstraint(
            model_name='bankstatementline',
            constraint=models.UniqueConstraint(fields=('statement', 'sequence'), name='bank_line_sequence_unique'),
        ),
        migrations.AddConstraint(
            model_name='bankstatementline',
            constraint=models.CheckConstraint(check=models.Q(('amount', 0), _negated=True), name='bank_line_amount_nonzero'),
        ),
        migrations.AddConstraint(
            model_name='bankstatement',
            constraint=models.CheckConstraint(check=models.Q(('end_date__gte', models.F('start_date'))), name='bank_statement_period_valid'),
        ),
        migrations.AddConstraint(
            model_name='bankstatement',
            constraint=models.CheckConstraint(check=models.Q(('currency__in', ('EUR',))), name='bank_statement_currency_supported'),
        ),
        migrations.RunPython(execute(0), execute(1)),
    ]
