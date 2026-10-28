from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import JournalEntry

User = get_user_model()


def post_journal_entry(entry: JournalEntry, *, posted_by: User | None = None) -> JournalEntry:
    """Validate and atomically post a balanced draft journal entry."""
    with transaction.atomic():
        locked_entry = (
            JournalEntry.objects.select_for_update()
            .prefetch_related("lines")
            .get(pk=entry.pk)
        )
        if locked_entry.status != JournalEntry.Status.DRAFT:
            raise ValidationError("Only draft journal entries can be posted.")

        lines = list(locked_entry.lines.all())
        if not lines:
            raise ValidationError("A journal entry requires at least one line.")

        currencies = {line.currency for line in lines}
        if len(currencies) != 1:
            raise ValidationError("A journal entry must use one currency.")

        total_debits = sum((line.debit for line in lines), Decimal("0.00"))
        total_credits = sum((line.credit for line in lines), Decimal("0.00"))
        if total_debits != total_credits:
            raise ValidationError("Journal entry debits and credits must balance.")

        locked_entry.status = JournalEntry.Status.POSTED
        locked_entry.posted_at = timezone.now()
        if posted_by is not None:
            locked_entry.posted_by = posted_by
        locked_entry.save(update_fields=["status", "posted_at", "posted_by", "updated_at"])
        return locked_entry
