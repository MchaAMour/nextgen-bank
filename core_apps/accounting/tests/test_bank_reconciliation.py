from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from core_apps.accounting.bank_reconciliation import (auto_match, bank_reconciliation, import_bank_statement,
    match_line, parse_statement_csv, post_statement_line, unmatch_line)
from core_apps.accounting.models import BankMatch, BankStatement, BankStatementLine, JournalLine
from core_apps.accounting.services import post_invoice, post_lines, record_customer_payment
from .test_api import client, user
from .test_ar_integrity import db_id, new_id, raw
from .test_ar_workflow import invoice
from .test_ar_write_api import grant

pytestmark = pytest.mark.django_db
BASE = "/api/v1/accounting/"
ZERO = Decimal("0")


def bank_payment(invoice, amount="100", reference="BT-1", received_on=date(2026, 1, 20)):
    return record_customer_payment(invoice.customer, amount=amount, currency="EUR", reference=reference,
                                   received_on=received_on)


def stripe_payout(amount, entry_date):
    amount = Decimal(amount)
    return post_lines([("1000", amount, ZERO), ("1010", ZERO, amount)], currency="EUR",
                      source_type="stripe_balance_transaction", source_id=None, entry_date=entry_date,
                      memo="Stripe payout txn_1")


def bank_line(entry, code="1000"):
    return entry.lines.get(account__code=code)


def statement(reference="ST-1", start=date(2026, 1, 1), end=date(2026, 1, 31), opening="0.00", lines=(),
              **kwargs):
    lines = [{"booked_on": booked_on, "amount": amount, "description": description, "bank_reference": ref}
             for booked_on, amount, description, ref in lines]
    closing = kwargs.pop("closing", None)
    if closing is None:
        closing = str(Decimal(opening) + sum(Decimal(line["amount"]) for line in lines))
    return import_bank_statement(reference=reference, start_date=start, end_date=end, opening_balance=opening,
                                 closing_balance=closing, lines=lines, **kwargs)


JANUARY = [(date(2026, 1, 21), "100.00", "Transfer from AR customer", "BT-1"),
           (date(2026, 1, 25), "96.80", "STRIPE PAYOUT", ""),
           (date(2026, 1, 31), "-4.50", "Account fee", "")]


@pytest.fixture
def books(invoice):
    post_invoice(invoice)
    payment = bank_payment(invoice)
    payout = stripe_payout("96.80", date(2026, 1, 22))
    return bank_line(payment.journal_entry), bank_line(payout)


def test_import_matches_ledger_lines_and_reconciles_after_posting_bank_charges(books):
    payment_line, payout_line = books
    january = statement(lines=JANUARY)
    lines = list(january.lines.order_by("sequence"))
    assert [line.match.journal_line for line in lines[:2]] == [payment_line, payout_line]
    assert {line.match.method for line in lines[:2]} == {"automatic"}
    assert not BankMatch.objects.filter(statement_line=lines[2]).exists()

    report = bank_reconciliation(as_of=date(2026, 1, 31))
    assert (report["bank_balance"], report["ledger_balance"]) == (Decimal("192.30"), Decimal("196.80"))
    assert report["difference"] == Decimal("-4.50") and report["unexplained"] == 0
    assert report["unmatched_bank_lines"]["count"] == 1 and report["reconciled"] is False

    match = post_statement_line(lines[2], account_code="5000", memo="Bank charges")
    assert match.method == "posted"
    entry = match.journal_line.entry
    assert {(l.account.code, l.debit, l.credit) for l in entry.lines.all()} == {
        ("5000", Decimal("4.50"), 0), ("1000", 0, Decimal("4.50"))}
    assert (entry.entry_date, entry.source_type) == (date(2026, 1, 31), "bank_statement_line")
    report = bank_reconciliation(as_of=date(2026, 1, 31))
    assert report["difference"] == 0 and report["reconciled"] is True


def test_identical_import_is_a_replay_and_changed_details_are_rejected(books):
    first = statement(lines=JANUARY)
    assert statement(lines=JANUARY) == first
    assert BankStatementLine.objects.count() == 3 and BankMatch.objects.count() == 2
    changed = [JANUARY[0], (date(2026, 1, 25), "96.81", "STRIPE PAYOUT", ""), JANUARY[2]]
    with pytest.raises(ValidationError, match="different details"):
        statement(lines=changed)


@pytest.mark.parametrize("kwargs, message", [
    ({"lines": [(date(2026, 1, 5), "10.00", "", "")], "closing": "11.00"}, "does not equal the closing"),
    ({"lines": [(date(2026, 2, 5), "10.00", "", "")]}, "within the statement period"),
    ({"lines": [(date(2026, 1, 5), "0", "", "")]}, "cannot be zero"),
    ({"lines": [(date(2026, 1, 5), 10.0, "", "")], "closing": "10.00"}, "Use a decimal"),
    ({"lines": [(date(2026, 1, 5), "10.001", "", "")], "closing": "10.00"}, "two decimal places"),
    ({"lines": []}, "at least one line"),
    ({"lines": [(date(2026, 1, 5), "10.00", "", "")], "account_code": "1010"}, "not a bank account"),
    ({"lines": [(date(2026, 1, 5), "10.00", "", "")], "end": date(2025, 12, 31)}, "cannot end before"),
])
def test_invalid_statements_are_rejected(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        statement(**kwargs)
    assert not BankStatement.objects.exists()


def test_statements_form_a_chain():
    statement(lines=[(date(2026, 1, 5), "10.00", "", "")])
    with pytest.raises(ValidationError, match="must follow ST-1"):
        statement("ST-2", start=date(2026, 1, 31), end=date(2026, 2, 28), opening="10.00",
                  lines=[(date(2026, 2, 5), "1.00", "", "")])
    with pytest.raises(ValidationError, match=r"closing balance of ST-1 \(10.00\)"):
        statement("ST-2", start=date(2026, 2, 1), end=date(2026, 2, 28), opening="0.00",
                  lines=[(date(2026, 2, 5), "1.00", "", "")])
    statement("ST-2", start=date(2026, 2, 1), end=date(2026, 2, 28), opening="10.00",
              lines=[(date(2026, 2, 5), "1.00", "", "")])


def test_ambiguous_candidates_stay_unmatched_unless_reference_or_date_decides(invoice):
    post_invoice(invoice)
    early, late = (bank_line(stripe_payout("50.00", day)) for day in (date(2026, 1, 8), date(2026, 1, 12)))
    january = statement(lines=[(date(2026, 1, 10), "50.00", "Deposit", ""),
                               (date(2026, 1, 13), "50.00", "Deposit", "")])
    first, second = january.lines.order_by("sequence")
    # Both payouts are two days from the first deposit; the second is nearest to the later one.
    assert not BankMatch.objects.filter(statement_line=first).exists()
    assert second.match.journal_line == late
    assert auto_match() == {"matched": 1, "unmatched": 0}
    first.refresh_from_db()
    assert first.match.journal_line == early


def test_bank_reference_found_in_the_entry_decides_between_candidates(invoice):
    post_invoice(invoice)
    for reference in ("BT-1", "BT-2"):
        bank_payment(invoice, amount="30", reference=reference)
    january = statement(lines=[(date(2026, 1, 21), "30.00", "Transfer", "bt-2")])
    match = january.lines.get().match
    assert match.journal_line.entry.memo.endswith("BT-2")


def test_manual_match_and_unmatch(books):
    payment_line, payout_line = books
    january = statement(lines=[(date(2026, 1, 28), "100.00", "Late transfer", "")])
    line = january.lines.get()
    payment_match = BankMatch.objects.filter(statement_line=line).first()
    assert payment_match.journal_line == payment_line  # Within the window; auto-matched.
    with pytest.raises(ValidationError, match="already matched"):
        match_line(line, journal_line=payout_line)
    unmatch_line(line)
    with pytest.raises(ValidationError, match="statement line's amount"):
        match_line(line, journal_line=payout_line)
    with pytest.raises(ValidationError, match="posted ledger line of the statement's bank account"):
        match_line(line, journal_line=payment_line.entry.lines.get(account__code="1100"))
    match = match_line(line, journal_line=payment_line)
    assert match.method == "manual"
    with pytest.raises(ValidationError, match="not matched"):
        unmatch_line(BankStatementLine(pk=new_id()))


def test_control_accounts_cannot_be_posted_from_the_bank(books):
    line = statement(lines=JANUARY).lines.get(sequence=3)
    for code in ("1100", "1010", "1000"):
        with pytest.raises(ValidationError, match="own workflow"):
            post_statement_line(line, account_code=code, memo="")
    assert not BankMatch.objects.filter(statement_line=line).exists()


def test_timing_differences_across_statements(invoice):
    post_invoice(invoice)
    payment_line = bank_line(bank_payment(invoice, received_on=date(2026, 1, 30)).journal_entry)
    statement(lines=[(date(2026, 1, 5), "20.00", "Opening deposit", "")], opening="0.00")
    # The deposit has no ledger entry; the payment arrives at the bank in February.
    report = bank_reconciliation(as_of=date(2026, 1, 31))
    assert report["unmatched_bank_lines"]["total"] == Decimal("20.00")
    assert report["unmatched_ledger_lines"]["items"][0]["id"] == payment_line.pk
    assert report["unexplained"] == 0
    february = statement("ST-2", start=date(2026, 2, 1), end=date(2026, 2, 28), opening="20.00",
                         lines=[(date(2026, 2, 2), "100.00", "Transfer", "")])
    assert february.lines.get().match.journal_line == payment_line
    # Matched to a February bank line, the payment is still outstanding on 31 January.
    assert bank_reconciliation(as_of=date(2026, 1, 31))["unmatched_ledger_lines"]["count"] == 1
    report = bank_reconciliation(as_of=date(2026, 2, 28))
    assert report["unmatched_ledger_lines"]["count"] == 0 and report["statements_cover_to"] == date(2026, 2, 28)


def test_opening_balance_without_ledger_history_is_reported(invoice):
    statement(opening="50.00", lines=[(date(2026, 1, 5), "-5.00", "Fee", "")])
    report = bank_reconciliation(as_of=date(2026, 1, 31))
    assert report["opening_difference"] == Decimal("50.00")
    assert report["difference"] == Decimal("45.00") and report["unexplained"] == 0
    with pytest.raises(ValidationError, match="No bank statement"):
        bank_reconciliation(as_of=date(2025, 12, 31))


def test_parse_csv():
    text = "﻿Date,Amount,Description,Reference\n2026-01-21,100.00,Transfer,BT-1\n2026-01-31,-4.50,Fee,\n"
    assert parse_statement_csv(text) == [
        {"booked_on": date(2026, 1, 21), "amount": "100.00", "description": "Transfer", "bank_reference": "BT-1"},
        {"booked_on": date(2026, 1, 31), "amount": "-4.50", "description": "Fee", "bank_reference": ""}]
    with pytest.raises(ValidationError, match="columns"):
        parse_statement_csv("date,amount\n2026-01-01,1\n")
    with pytest.raises(ValidationError, match="Row 2"):
        parse_statement_csv("date,amount,description,reference\n21/01/2026,1,,\n")


def test_database_rejects_changes_that_bypass_the_services(books):
    payment_line, payout_line = books
    january = statement(lines=JANUARY)
    line = january.lines.get(sequence=3)
    for sql in ("UPDATE accounting_bankstatementline SET amount = 1 WHERE id = %s",
                "DELETE FROM accounting_bankstatementline WHERE id = %s"):
        with pytest.raises(IntegrityError):
            raw(sql, db_id(line))
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_bankstatement SET closing_balance = 0 WHERE id = %s", db_id(january))
    insert = ("INSERT INTO accounting_bankmatch (id, created_at, updated_at, method, journal_line_id,"
              " statement_line_id) VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'manual', %s, %s)")
    fee_entry = post_lines([("5000", Decimal("4.51"), ZERO), ("1000", ZERO, Decimal("4.51"))], currency="EUR",
                           source_type="", source_id=None, entry_date=date(2026, 1, 31), memo="Wrong amount")
    with pytest.raises(IntegrityError):
        raw(insert, new_id(), db_id(bank_line(fee_entry)), db_id(line))
    with pytest.raises(IntegrityError):
        raw(insert, new_id(), db_id(fee_entry.lines.get(account__code="5000")), db_id(line))
    match = january.lines.get(sequence=1).match
    with pytest.raises(IntegrityError):
        raw("UPDATE accounting_bankmatch SET journal_line_id = %s WHERE id = %s", db_id(payout_line), db_id(match))
    raw("DELETE FROM accounting_bankmatch WHERE id = %s", db_id(match))  # Unmatching is allowed.
    with pytest.raises(ValidationError):
        BankStatementLine.objects.all().delete()
    with pytest.raises(ValidationError):
        BankMatch.objects.all().delete()


READ = ["view_bankstatement", "view_bankstatementline", "view_bankmatch"]
IMPORT = ["add_bankstatement", "add_bankstatementline", "add_bankmatch"]


def payload(**changes):
    return {"reference": "ST-API", "start_date": "2026-01-01", "end_date": "2026-01-31",
            "opening_balance": "0.00", "closing_balance": "192.30",
            "lines": [{"booked_on": str(day), "amount": amount, "description": text, "bank_reference": ref}
                      for day, amount, text, ref in JANUARY], **changes}


def test_api_import_match_post_and_report(client, user, books):
    url = BASE + "bank-statements/"
    assert client.post(url, payload(), format="json").status_code == 403
    grant(user, *IMPORT, "delete_bankmatch", "add_journalentry")
    user = type(user).objects.get(pk=user.pk)
    client.force_authenticate(user)
    created = client.post(url, payload(), format="json")
    assert created.status_code == 201
    assert [line["match"] is not None for line in created.data["lines"]] == [True, True, False]
    assert client.post(url, payload(), format="json").status_code == 200
    numbers = payload(closing_balance=192.30)
    assert client.post(url, numbers, format="json").status_code == 400

    detail = client.get(f"{url}{created.data['id']}/")
    assert detail.status_code == 200 and detail.data["account"] == "1000"
    fee_line = created.data["lines"][2]["id"]
    posted = client.post(f"{BASE}bank-lines/{fee_line}/post/", {"account": "5000", "memo": "Charges"},
                         format="json")
    assert posted.status_code == 201 and posted.data["match"]["method"] == "posted"
    report = client.get(BASE + "bank-reconciliation/", {"as_of": "2026-01-31"})
    assert report.status_code == 200
    assert (report.data["difference"], report.data["reconciled"]) == ("0.00", True)

    payment_line = created.data["lines"][0]
    unmatch = client.delete(f"{BASE}bank-lines/{payment_line['id']}/match/")
    assert unmatch.status_code == 200 and unmatch.data["match"] is None
    rematch = client.post(f"{BASE}bank-lines/{payment_line['id']}/match/",
                          {"journal_line": payment_line["match"]["journal_line"]}, format="json")
    assert rematch.status_code == 201 and rematch.data["match"]["method"] == "manual"
    assert client.post(BASE + "bank-statements/auto-match/").data == {"matched": 0, "unmatched": 0}


def test_api_csv_import(client, user, books):
    grant(user, *IMPORT)
    user = type(user).objects.get(pk=user.pk)
    client.force_authenticate(user)
    csv_text = "date,amount,description,reference\n" + "".join(
        f"{day},{amount},{text},{ref}\n" for day, amount, text, ref in JANUARY)
    upload = SimpleUploadedFile("january.csv", csv_text.encode(), content_type="text/csv")
    data = {key: value for key, value in payload().items() if key != "lines"}
    response = client.post(BASE + "bank-statements/import-csv/", {**data, "file": upload}, format="multipart")
    assert response.status_code == 201 and len(response.data["lines"]) == 3
    bad = SimpleUploadedFile("bad.csv", b"date,amount\n", content_type="text/csv")
    response = client.post(BASE + "bank-statements/import-csv/", {**data, "reference": "X", "file": bad},
                           format="multipart")
    assert response.status_code == 400


def test_api_read_permissions(client, user):
    assert client.get(BASE + "bank-statements/").status_code == 200  # The client fixture has view_*.
    assert client.get(BASE + "bank-reconciliation/").status_code == 400  # No statement yet.
