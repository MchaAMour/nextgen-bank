from django.core.management.base import BaseCommand

from core_apps.accounting.roles import setup_roles


class Command(BaseCommand):
    help = "Create or reset the finance roles (Django Groups) with least-privilege accounting permissions."

    def handle(self, *args, **options):
        for name, count in setup_roles().items():
            self.stdout.write(f"{name}: {count} permissions")
