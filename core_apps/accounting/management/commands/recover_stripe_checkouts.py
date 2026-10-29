from django.core.management.base import BaseCommand

from core_apps.accounting.stripe_payments import recover_checkout_sessions


class Command(BaseCommand):
    help = "Record paid or expired Checkout Sessions whose webhook never arrived, reading them from Stripe."

    def handle(self, *args, **options):
        moved = recover_checkout_sessions()
        self.stdout.write(f"recovered: {moved or 'nothing to recover'}")
