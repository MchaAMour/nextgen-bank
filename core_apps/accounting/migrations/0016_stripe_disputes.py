"""Stripe disputes (STRIPE-02): disputed payments (1150) and chargeback losses (5110).

A dispute's withdrawal moves its amount from Stripe clearing to disputed payments;
a won dispute moves it back, and a lost one is written off to chargeback losses
through the balance transaction's loss entry.
"""

from django.db import migrations, models
import django.db.models.deletion

ACCOUNTS = [
    ("1150", "Disputed Stripe Payments", "asset", "debit"),
    ("5110", "Chargeback Losses", "expense", "debit"),
]


def seed_accounts(apps, schema_editor):
    ChartOfAccount = apps.get_model("accounting", "ChartOfAccount")
    for code, name, account_type, normal_balance in ACCOUNTS:
        ChartOfAccount.objects.get_or_create(code=code, defaults={
            "name": name, "account_type": account_type, "normal_balance": normal_balance, "is_active": True})


def remove_accounts(apps, schema_editor):
    apps.get_model("accounting", "ChartOfAccount").objects.filter(
        code__in=[code for code, *_ in ACCOUNTS], journal_lines__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0015_allocation_release'),
    ]

    operations = [
        migrations.AddField(
            model_name='stripebalancetransaction',
            name='loss_entry',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='stripe_dispute_loss', to='accounting.journalentry'),
        ),
        migrations.RunPython(seed_accounts, remove_accounts),
    ]
