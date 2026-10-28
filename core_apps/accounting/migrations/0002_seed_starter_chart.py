from django.db import migrations


STARTER_ACCOUNTS = [
    ("1000", "Cash and Bank", "asset", "debit"),
    ("1010", "Stripe Clearing", "asset", "debit"),
    ("1100", "Accounts Receivable", "asset", "debit"),
    ("2000", "Accounts Payable", "liability", "credit"),
    ("2100", "Tax Payable", "liability", "credit"),
    ("3000", "Retained Earnings", "equity", "credit"),
    ("4000", "Sales Revenue", "income", "credit"),
    ("5000", "Operating Expenses", "expense", "debit"),
]


def seed_starter_chart(apps, schema_editor):
    chart_of_account = apps.get_model("accounting", "ChartOfAccount")
    for code, name, account_type, normal_balance in STARTER_ACCOUNTS:
        chart_of_account.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "normal_balance": normal_balance,
                "is_active": True,
            },
        )


def remove_starter_chart(apps, schema_editor):
    chart_of_account = apps.get_model("accounting", "ChartOfAccount")
    chart_of_account.objects.filter(code__in=[account[0] for account in STARTER_ACCOUNTS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounting", "0001_initial")]

    operations = [migrations.RunPython(seed_starter_chart, remove_starter_chart)]
