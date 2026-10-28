
"""Stripe Checkout sessions and webhook events (Goal 4).

Payments recorded from Stripe debit Stripe clearing (1010) instead of cash and
bank (1000), so the payment insert trigger from 0006 now accepts either debit
account. The credit leg must still be AR (1100) for the payment amount.
"""

import importlib

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


AR_0006 = importlib.import_module("core_apps.accounting.migrations.0006_ar_settlements")
CASH_DEBIT = "a.code = '1000' AND l.debit"
CLEARING_OR_CASH_DEBIT = "a.code IN ('1000', '1010') AND l.debit"
assert AR_0006.PAYMENT_OK.count(CASH_DEBIT) == 1
PAYMENT_OK = AR_0006.PAYMENT_OK.replace(CASH_DEBIT, CLEARING_OR_CASH_DEBIT)


def payment_trigger(payment_ok):
    sqlite = AR_0006.SQLITE_FORWARD[2]
    assert "accounting_customerpayment_guard_insert" in sqlite
    return {
        "sqlite": ["DROP TRIGGER accounting_customerpayment_guard_insert",
                   sqlite.replace(AR_0006.PAYMENT_OK, payment_ok)],
        "postgresql": [AR_0006.postgres_guard("customerpayment", AR_0006.postgres_reject(
            payment_ok, "Payment requires a posted entry debiting cash or Stripe clearing and crediting AR."))[0]
            .replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")],
    }


FORWARD, REVERSE = payment_trigger(PAYMENT_OK), payment_trigger(AR_0006.PAYMENT_OK)


def execute(statements):
    def run(apps, schema_editor):
        for statement in statements[schema_editor.connection.vendor]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounting', '0006_ar_settlements'),
    ]

    operations = [
        migrations.CreateModel(
            name='StripeWebhookEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event_id', models.CharField(max_length=255, unique=True)),
                ('event_type', models.CharField(max_length=100)),
                ('livemode', models.BooleanField()),
                ('payload', models.JSONField()),
                ('status', models.CharField(choices=[('received', 'Received'), ('processed', 'Processed'), ('ignored', 'Ignored'), ('failed', 'Failed')], default='received', max_length=10)),
                ('error', models.TextField(blank=True)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='StripeCheckoutSession',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('stripe_session_id', models.CharField(max_length=255, unique=True)),
                ('url', models.URLField(max_length=2000)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('currency', models.CharField(max_length=3)),
                ('status', models.CharField(choices=[('open', 'Open'), ('awaiting_payment', 'Completed, awaiting asynchronous payment'), ('paid', 'Paid'), ('failed', 'Payment failed'), ('expired', 'Expired')], default='open', max_length=20)),
                ('expires_at', models.DateTimeField()),
                ('payment_intent_id', models.CharField(blank=True, max_length=255)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='stripe_checkout_sessions', to=settings.AUTH_USER_MODEL)),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='stripe_sessions', to='accounting.invoice')),
                ('payment', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stripe_session', to='accounting.customerpayment')),
            ],
        ),
        migrations.AddConstraint(
            model_name='stripecheckoutsession',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='stripe_session_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='stripecheckoutsession',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['open', 'awaiting_payment'])), fields=('invoice',), name='stripe_session_one_active_per_invoice'),
        ),
        migrations.AddConstraint(
            model_name='stripecheckoutsession',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('payment__isnull', False), ('status', 'paid')), models.Q(models.Q(('status', 'paid'), _negated=True), ('payment__isnull', True)), _connector='OR'), name='stripe_session_paid_has_payment'),
        ),
        migrations.RunPython(execute(FORWARD), execute(REVERSE)),
    ]
