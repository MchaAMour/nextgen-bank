from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from core_apps.accounting.tasks import SYNC_TASK

NAME = "Stripe balance sync"


class Command(BaseCommand):
    help = "Create or update the periodic Celery beat task that syncs Stripe balance transactions."

    def add_arguments(self, parser):
        parser.add_argument("--every-minutes", type=int, default=60)
        parser.add_argument("--disable", action="store_true", help="Keep the task but stop running it.")

    @transaction.atomic
    def handle(self, *args, every_minutes, disable, **options):
        if every_minutes < 5:
            raise CommandError("Sync at most every 5 minutes; each run lists a day of transactions.")
        interval, _ = IntervalSchedule.objects.get_or_create(every=every_minutes, period=IntervalSchedule.MINUTES)
        task, created = PeriodicTask.objects.update_or_create(name=NAME, defaults={
            "task": SYNC_TASK, "interval": interval, "crontab": None, "enabled": not disable})
        state = "disabled" if disable else f"every {every_minutes} minutes"
        self.stdout.write(f"{'Created' if created else 'Updated'} '{NAME}': {state}.")
