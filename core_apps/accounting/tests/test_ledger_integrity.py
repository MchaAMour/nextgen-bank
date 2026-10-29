import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction

from core_apps.accounting.models import AuditEvent, ChartOfAccount, Invoice, JournalEntry, JournalLine
from core_apps.accounting.services import (cancel_invoice, post_invoice, post_journal_entry, post_lines,
    reverse_journal_entry)
from .test_ar_integrity import db_id, new_id, raw
from .test_ar_workflow import invoice

ZERO = Decimal("0")
DAY = date(2026, 1, 10)


def entry(*lines, entry_date=DAY):
    return post_lines(lines or [("5000", Decimal("10"), ZERO), ("1000", ZERO, Decimal("10"))], currency="EUR",
                      source_type="", source_id=None, entry_date=entry_date, memo="Manual")


def draft(*amounts):
    draft = JournalEntry.objects.create(entry_date=DAY, memo="Draft")
    accounts = {a.code: a for a in ChartOfAccount.objects.filter(code__in=["5000", "1000"])}
    for code, debit, credit in amounts:
        JournalLine.objects.create(entry=draft, account=accounts[code], debit=debit, credit=credit)
    return draft


def insert_line(entry_id, account, debit, credit):
    raw("INSERT INTO accounting_journalline (id, created_at, updated_at, entry_id, account_id, description,"
        " debit, credit, currency) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, '', %s, %s, 'EUR')",
        new_id(), entry_id, db_id(account), debit, credit)


@pytest.mark.django_db
class TestDatabaseGuards:
    def test_entries_are_inserted_as_drafts(self):
        with pytest.raises(IntegrityError):
            raw("INSERT INTO accounting_journalentry (id, created_at, updated_at, entry_date, memo, reference,"
                " source_type, status) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, '', '', '', 'posted')",
                new_id(), DAY)

    @pytest.mark.parametrize("amounts", [
        [],
        [("5000", Decimal("10"), ZERO)],
        [("5000", Decimal("10"), ZERO), ("1000", ZERO, Decimal("9.99"))],
    ])
    def test_only_balanced_drafts_with_two_lines_can_be_posted(self, amounts):
        unbalanced = draft(*amounts)
        with pytest.raises(IntegrityError):
            raw("UPDATE accounting_journalentry SET status = 'posted', posted_at = CURRENT_TIMESTAMP WHERE id = %s",
                db_id(unbalanced))

    def test_a_balanced_draft_can_be_posted(self):
        balanced = draft(("5000", Decimal("0.10"), ZERO), ("5000", Decimal("0.20"), ZERO),
                         ("1000", ZERO, Decimal("0.30")))
        raw("UPDATE accounting_journalentry SET status = 'posted', posted_at = CURRENT_TIMESTAMP WHERE id = %s",
            db_id(balanced))
        balanced.refresh_from_db()
        assert balanced.status == "posted"

    @pytest.mark.parametrize("sql", [
        "UPDATE accounting_journalentry SET memo = 'changed' WHERE id = %s",
        "UPDATE accounting_journalentry SET status = 'draft' WHERE id = %s",
        "UPDATE accounting_journalentry SET status = 'reversed' WHERE id = %s",
        "DELETE FROM accounting_journalentry WHERE id = %s",
        "UPDATE accounting_journalline SET debit = 11 WHERE entry_id = %s",
        "DELETE FROM accounting_journalline WHERE entry_id = %s",
    ])
    def test_posted_entries_and_lines_are_immutable(self, sql):
        posted = entry()
        with pytest.raises(IntegrityError):
            raw(sql, db_id(posted))

    def test_lines_cannot_be_added_to_or_moved_into_posted_entries(self):
        posted, open_draft = entry(), draft(("5000", Decimal("1"), ZERO))
        account = ChartOfAccount.objects.get(code="5000")
        with pytest.raises(IntegrityError):
            insert_line(db_id(posted), account, "1.00", "0")
        with pytest.raises(IntegrityError):
            raw("UPDATE accounting_journalline SET entry_id = %s WHERE entry_id = %s", db_id(posted),
                db_id(open_draft))
        raw("UPDATE accounting_journalline SET debit = 2 WHERE entry_id = %s", db_id(open_draft))

    @pytest.mark.parametrize("debit, credit", [("5.00", "5.00"), ("0", "0")])
    def test_each_line_has_exactly_one_positive_side(self, debit, credit):
        with pytest.raises(IntegrityError):
            insert_line(db_id(draft()), ChartOfAccount.objects.get(code="5000"), debit, credit)

    def test_accounts_with_posted_lines_keep_their_code_and_type(self):
        entry()
        account = ChartOfAccount.objects.get(code="5000")
        for sql in ("UPDATE accounting_chartofaccount SET code = '5001' WHERE id = %s",
                    "UPDATE accounting_chartofaccount SET account_type = 'asset', normal_balance = 'debit'"
                    " WHERE id = %s"):
            with pytest.raises(IntegrityError):
                raw(sql, db_id(account))
        account.account_type = "liability"
        with pytest.raises(ValidationError, match="cannot change"):
            account.save()
        account = ChartOfAccount.objects.get(code="5000")
        account.name, account.is_active = "General expenses", False
        account.save()
        unused = ChartOfAccount.objects.create(code="6100", name="Unused", account_type="expense")
        draft_only = JournalEntry.objects.create(entry_date=DAY)
        JournalLine.objects.create(entry=draft_only, account=unused, debit=1)
        unused.account_type = "asset"
        unused.save()  # Only draft lines; nothing posted is reinterpreted.


@pytest.mark.django_db
class TestReversal:
    def test_reversal_swaps_lines_and_marks_the_original(self):
        original = entry(("5000", Decimal("12.50"), ZERO), ("5100", Decimal("2.50"), ZERO),
                         ("1000", ZERO, Decimal("15.00")))
        reversal = reverse_journal_entry(original, entry_date=date(2026, 1, 12), memo="Wrong supplier")
        original.refresh_from_db()
        assert (original.status, reversal.status, reversal.reversal_of) == ("reversed", "posted", original)
        assert {(l.account.code, l.debit, l.credit) for l in reversal.lines.all()} == {
            ("5000", 0, Decimal("12.50")), ("5100", 0, Decimal("2.50")), ("1000", Decimal("15.00"), 0)}
        pair = JournalLine.objects.filter(entry__in=[original, reversal])
        for code in ("5000", "5100", "1000"):
            lines = pair.filter(account__code=code)
            assert sum(l.debit - l.credit for l in lines) == 0
        assert AuditEvent.objects.filter(action="journal_entry.reversed", object_id=str(original.pk)).exists()

    def test_reversal_rules(self):
        original = entry()
        with pytest.raises(ValidationError, match="cannot precede"):
            reverse_journal_entry(original, entry_date=date(2026, 1, 9))
        with pytest.raises(ValidationError, match="Only posted"):
            reverse_journal_entry(draft(), entry_date=DAY)
        reverse_journal_entry(original, entry_date=DAY)
        with pytest.raises(ValidationError, match="Only posted"):
            reverse_journal_entry(original, entry_date=DAY)
        with pytest.raises(IntegrityError), transaction.atomic():  # Nor can it be marked again directly.
            JournalEntry.objects.get(pk=original.pk)._mark_reversed()

    def test_reversal_may_use_an_account_deactivated_since(self):
        original = entry()
        account = ChartOfAccount.objects.get(code="5000")
        account.is_active = False
        account.save()
        with pytest.raises(ValidationError, match="inactive"):
            entry()
        reverse_journal_entry(original, entry_date=DAY)

    def test_failed_reversal_leaves_the_original_posted(self):
        original = entry()
        with patch_post_lines_failure():
            with pytest.raises(RuntimeError):
                reverse_journal_entry(original, entry_date=DAY)
        original.refresh_from_db()
        assert original.status == "posted" and not JournalEntry.objects.filter(reversal_of=original).exists()

    def test_invoice_cancellation_reverses_its_entry(self, invoice):
        posted = post_invoice(invoice)
        cancelled = cancel_invoice(posted, cancellation_date=date(2026, 1, 3))
        original = Invoice.objects.get(pk=posted.pk).journal_entry
        assert (original.status, cancelled.cancellation_entry.reversal_of) == ("reversed", original)
        assert cancelled.cancellation_entry.source_type == "invoice"


def patch_post_lines_failure():
    from unittest.mock import patch

    from core_apps.accounting import services

    real = services.post_lines

    def failing(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("simulated failure after posting the reversal")

    return patch.object(services, "post_lines", failing)


@pytest.fixture
def starter_chart():
    """Transactional tests truncate tables between tests, including the chart that migrations seed."""
    import importlib
    seed = importlib.import_module("core_apps.accounting.migrations.0002_seed_starter_chart")
    for code, name, account_type, _ in seed.STARTER_ACCOUNTS:
        ChartOfAccount.objects.get_or_create(code=code, defaults={"name": name, "account_type": account_type})


postgres_only = pytest.mark.skipif(connection.vendor != "postgresql",
                                   reason="Row locks need PostgreSQL; SQLite serializes all writers.")


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_line_insert_waits_for_a_concurrent_posting_and_is_then_rejected(starter_chart):
    """INT-02: two connections. Posting holds the entry lock; a raw line insert waits, then fails."""
    open_draft = draft(("5000", Decimal("10"), ZERO), ("1000", ZERO, Decimal("10")))
    account = ChartOfAccount.objects.get(code="5000")
    posting_started, outcome = threading.Event(), {}

    def post_slowly():
        with transaction.atomic():
            locked = JournalEntry.objects.select_for_update().get(pk=open_draft.pk)
            posting_started.set()
            time.sleep(1.0)
            post_journal_entry(locked)
        connections.close_all()

    def insert_concurrently():
        posting_started.wait()
        started = time.monotonic()
        try:
            insert_line(db_id(open_draft), account, "1.00", "0")
            outcome["error"] = None
        except IntegrityError as error:
            outcome["error"] = error
        outcome["waited"] = time.monotonic() - started
        connections.close_all()

    threads = [threading.Thread(target=post_slowly), threading.Thread(target=insert_concurrently)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert isinstance(outcome["error"], IntegrityError)
    assert outcome["waited"] >= 0.8
    assert JournalLine.objects.filter(entry=open_draft).count() == 2


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_allocations_cannot_overpay(starter_chart, invoice):
    """PG-01: two connections allocate 60 each against a 100 invoice; exactly one succeeds."""
    from core_apps.accounting.services import allocate_payment, record_customer_payment

    posted = post_invoice(invoice)
    payments = [record_customer_payment(posted.customer, amount="60", currency="EUR", reference=f"RACE-{n}",
                                        received_on=date(2026, 1, 2)) for n in (1, 2)]
    barrier, results = threading.Barrier(2), []

    def allocate(payment):
        barrier.wait()
        try:
            allocate_payment(payment, invoice=posted, amount="60", reference=f"{payment.reference}-A",
                             allocated_on=date(2026, 1, 2))
            results.append("ok")
        except (ValidationError, IntegrityError):
            results.append("rejected")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=allocate, args=(payment,)) for payment in payments]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "rejected"]
    assert Invoice.objects.with_balances().get(pk=posted.pk).open_balance == Decimal("40")


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_draft_waits_for_a_concurrent_deactivation_and_is_rejected(starter_chart):
    """AR-02: creating a draft re-reads the customer under a row lock, so it sees a committed deactivation."""
    from core_apps.accounting.models import Customer
    from core_apps.accounting.services import create_draft_invoice

    customer = Customer.objects.create(name="Leaving")
    deactivating, outcome = threading.Event(), {}

    def deactivate():
        with transaction.atomic():
            locked = Customer.objects.select_for_update().get(pk=customer.pk)
            locked.is_active = False
            locked.save()
            deactivating.set()
            time.sleep(1.0)
        connections.close_all()

    def draft_invoice():
        deactivating.wait()
        started = time.monotonic()
        try:
            create_draft_invoice(customer=customer, number="RACE-INV", issue_date=DAY, due_date=DAY,
                                 lines=[dict(description="Work", quantity="1", unit_price="10")])
            outcome["error"] = None
        except ValidationError as error:
            outcome["error"] = error
        outcome["waited"] = time.monotonic() - started
        connections.close_all()

    threads = [threading.Thread(target=deactivate), threading.Thread(target=draft_invoice)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert "inactive customer" in str(outcome["error"]) and outcome["waited"] >= 0.8
    assert not Invoice.objects.filter(number="RACE-INV").exists()


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_reuse_of_a_payment_reference_creates_one_payment(starter_chart, invoice):
    """AR-04: both requests pass the replay check; the unique reference lets one commit, the other rolls back."""
    from core_apps.accounting.models import CustomerPayment
    from core_apps.accounting.services import record_customer_payment

    barrier, results = threading.Barrier(2), []

    def pay(amount):
        barrier.wait()
        try:
            record_customer_payment(invoice.customer, amount=amount, currency="EUR", reference="SAME-REF",
                                    received_on=DAY)
            results.append("ok")
        except IntegrityError:
            results.append("conflict")  # The API answers 409; the client retries and gets the replay.
        except ValidationError:
            results.append("replay mismatch")  # The other request committed first.
        finally:
            connections.close_all()

    threads = [threading.Thread(target=pay, args=(amount,)) for amount in ("10", "20")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1 and len(results) == 2
    payment = CustomerPayment.objects.get(reference="SAME-REF")
    assert JournalEntry.objects.filter(source_type="customer_payment").count() == 1
    assert payment.journal_entry.status == "posted"
