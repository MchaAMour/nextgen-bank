"""Stripe balance transactions and their reconciliation with Stripe clearing (Goal 6).

Adds the Payment Processing Fees expense account (5100) for Stripe fees. Refunds
made through Stripe credit Stripe clearing (1010) rather than cash and bank
(1000), so the refund insert trigger from 0006 now accepts either credit account.
"""

import importlib

from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import uuid

AR_0006 = importlib.import_module("core_apps.accounting.migrations.0006_ar_settlements")
CASH_CREDIT = "a.code = '1000' AND l.credit"
CLEARING_OR_CASH_CREDIT = "a.code IN ('1000', '1010') AND l.credit"
assert AR_0006.REFUND_OK.count(CASH_CREDIT) == 1
REFUND_OK = AR_0006.REFUND_OK.replace(CASH_CREDIT, CLEARING_OR_CASH_CREDIT)
FEE_ACCOUNT = ("5100", "Payment Processing Fees", "expense", "debit")


def refund_trigger(refund_ok):
    sqlite, postgres = AR_0006.SQLITE_FORWARD[4], AR_0006.POSTGRES_FORWARD[5]
    assert "accounting_paymentrefund_guard_insert" in sqlite and "accounting_paymentrefund_guard()" in postgres
    return {
        "sqlite": ["DROP TRIGGER accounting_paymentrefund_guard_insert",
                   sqlite.replace(AR_0006.REFUND_OK, refund_ok)],
        "postgresql": [postgres.replace(AR_0006.REFUND_OK, refund_ok)
                       .replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")],
    }


FORWARD, REVERSE = refund_trigger(REFUND_OK), refund_trigger(AR_0006.REFUND_OK)


def execute(statements):
    def run(apps, schema_editor):
        for statement in statements[schema_editor.connection.vendor]:
            # params=None sends PL/pgSQL without placeholder interpolation.
            schema_editor.execute(statement, params=None)

    return run


def seed_fee_account(apps, schema_editor):
    code, name, account_type, normal_balance = FEE_ACCOUNT
    apps.get_model("accounting", "ChartOfAccount").objects.get_or_create(code=code, defaults={
        "name": name, "account_type": account_type, "normal_balance": normal_balance, "is_active": True})


def remove_fee_account(apps, schema_editor):
    apps.get_model("accounting", "ChartOfAccount").objects.filter(code=FEE_ACCOUNT[0], journal_lines__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0008_eur_base_currency'),
    ]

    operations = [
        migrations.CreateModel(
            name='StripeBalanceTransaction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('stripe_id', models.CharField(max_length=255, unique=True)),
                ('reporting_category', models.CharField(max_length=50)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('fee', models.DecimalField(decimal_places=2, max_digits=18)),
                ('net', models.DecimalField(decimal_places=2, max_digits=18)),
                ('currency', models.CharField(max_length=3)),
                ('stripe_status', models.CharField(max_length=20)),
                ('stripe_created', models.DateTimeField()),
                ('source_id', models.CharField(blank=True, max_length=255)),
                ('state', models.CharField(choices=[('posted', 'Posted'), ('nothing_to_post', 'Matched, nothing to post'), ('needs_review', 'Needs review')], default='needs_review', max_length=20)),
                ('review_reason', models.CharField(blank=True, max_length=500)),
                ('payload', models.JSONField()),
                ('journal_entry', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stripe_balance_transaction', to='accounting.journalentry')),
                ('payment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stripe_balance_transactions', to='accounting.customerpayment')),
                ('refund', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stripe_balance_transaction', to='accounting.paymentrefund')),
            ],
        ),
        migrations.AddConstraint(
            model_name='stripebalancetransaction',
            constraint=models.CheckConstraint(check=models.Q(('net', django.db.models.expressions.CombinedExpression(models.F('amount'), '-', models.F('fee')))), name='stripe_balance_net_is_amount_less_fee'),
        ),
        migrations.AddConstraint(
            model_name='stripebalancetransaction',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('state', 'posted'), _negated=True), ('journal_entry__isnull', False), ('refund__isnull', False), _connector='OR'), name='stripe_balance_posted_has_entry'),
        ),
        migrations.AddConstraint(
            model_name='stripebalancetransaction',
            constraint=models.CheckConstraint(check=models.Q(('state', 'posted'), models.Q(('journal_entry__isnull', True), ('refund__isnull', True)), _connector='OR'), name='stripe_balance_only_posted_has_entry'),
        ),
        migrations.RunPython(seed_fee_account, remove_fee_account),
        migrations.RunPython(execute(FORWARD), execute(REVERSE)),
    ]
