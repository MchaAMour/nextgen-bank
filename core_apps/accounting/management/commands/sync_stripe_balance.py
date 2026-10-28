from django.core.management.base import BaseCommand

from core_apps.accounting.stripe_reconciliation import reconciliation_report, sync_balance_transactions


class Command(BaseCommand):
    help = "Sync Stripe balance transactions into the ledger and print the Stripe clearing reconciliation."

    def handle(self, *args, **options):
        for key, value in sync_balance_transactions().items():
            self.stdout.write(f"{key}: {value}")
        report = reconciliation_report()
        for key, value in report.items():
            self.stdout.write(f"{key}: {value}")
        if not report["reconciled"]:
            self.stdout.write(self.style.WARNING("Stripe clearing is not reconciled; review the items above."))
