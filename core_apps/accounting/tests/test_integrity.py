from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from core_apps.accounting.models import ChartOfAccount, JournalEntry, JournalLine
from core_apps.accounting.services import post_journal_entry

pytestmark = pytest.mark.django_db


@pytest.fixture
def posted():
    entry = JournalEntry.objects.create()
    line = JournalLine.objects.create(
        entry=entry, account=ChartOfAccount.objects.get(code="1000"), debit=Decimal("10")
    )
    JournalLine.objects.create(
        entry=entry, account=ChartOfAccount.objects.get(code="4000"), credit=Decimal("10")
    )
    post_journal_entry(entry)
    return entry, line


@pytest.mark.parametrize("model", [JournalEntry, JournalLine])
@pytest.mark.parametrize("operation", ["update", "delete", "bulk_update", "bulk_create"])
def test_bulk_writes_are_rejected(posted, model, operation):
    obj = posted[0] if model == JournalEntry else posted[1]
    with pytest.raises(ValidationError, match="Bulk journal"):
        if operation == "update":
            model.objects.filter(pk=obj.pk).update(id=obj.pk)
        elif operation == "delete":
            model.objects.filter(pk=obj.pk).delete()
        elif operation == "bulk_update":
            model.objects.bulk_update([obj], ["updated_at"])
        else:
            model.objects.bulk_create([obj])
    assert model.objects.filter(pk=obj.pk).exists()


def test_stale_draft_object_cannot_delete_posted_entry(posted):
    entry, _ = posted
    assert entry.status == JournalEntry.Status.DRAFT
    with pytest.raises(ValidationError, match="cannot be deleted"):
        entry.delete()


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_line_cannot_escape_posted_parent(posted, operation):
    entry, line = posted
    line.entry = JournalEntry.objects.create()
    with pytest.raises(ValidationError):
        getattr(line, operation)()
    line.refresh_from_db()
    assert line.entry_id == entry.pk


def test_draft_lines_remain_editable():
    entry = JournalEntry.objects.create()
    line = JournalLine.objects.create(
        entry=entry, account=ChartOfAccount.objects.get(code="1000"), debit=Decimal("10")
    )
    line.debit = Decimal("15")
    line.save()
    line.refresh_from_db()
    assert line.debit == Decimal("15")
    line.delete()
    entry.delete()
    assert not JournalEntry.objects.filter(pk=entry.pk).exists()
