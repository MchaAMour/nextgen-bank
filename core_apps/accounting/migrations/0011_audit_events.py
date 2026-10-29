"""Audit history: an append-only log of accounting actions written by the services (Goal 6).

Events cannot be updated or deleted, also through raw SQL.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

MESSAGE = "Audit events are immutable."
STATEMENTS = {
    "sqlite": ([
        f"""
        CREATE TRIGGER accounting_auditevent_guard_{operation} BEFORE {operation.upper()} ON accounting_auditevent
        BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END
        """ for operation in ("update", "delete")
    ], [f"DROP TRIGGER IF EXISTS accounting_auditevent_guard_{operation}" for operation in ("update", "delete")]),
    "postgresql": ([
        f"""
        CREATE FUNCTION accounting_auditevent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{MESSAGE}' USING ERRCODE = 'check_violation';
        END $$
        """,
        """
        CREATE TRIGGER accounting_auditevent_guard BEFORE UPDATE OR DELETE ON accounting_auditevent
        FOR EACH ROW EXECUTE FUNCTION accounting_auditevent_guard()
        """,
    ], [
        "DROP TRIGGER IF EXISTS accounting_auditevent_guard ON accounting_auditevent",
        "DROP FUNCTION IF EXISTS accounting_auditevent_guard()",
    ]),
}


def execute(direction):
    def run(apps, schema_editor):
        for statement in STATEMENTS[schema_editor.connection.vendor][direction]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounting', '0010_bank_statements'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('action', models.CharField(max_length=60)),
                ('object_type', models.CharField(max_length=60)),
                ('object_id', models.CharField(max_length=64)),
                ('summary', models.CharField(max_length=500)),
                ('details', models.JSONField(blank=True, default=dict)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='audit_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', 'id'],
                'indexes': [models.Index(fields=['object_type', 'object_id'], name='audit_event_object'), models.Index(fields=['created_at'], name='audit_event_created')],
            },
        ),
        migrations.RunPython(execute(0), execute(1)),
    ]
