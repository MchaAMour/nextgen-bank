# Bank reconciliation

Bank statements for cash and bank (1000) are imported, matched to the ledger and
reconciled. Statements never change the ledger by themselves; bank-only items
are posted explicitly.

## Statements

A statement has a unique reference, a period, opening and closing balances
and its lines (positive = money in, negative = money out). On import:

- Opening balance plus the lines must equal the closing balance, and every line
  must fall within the period.
- Statements of one account form a chain: each starts after the previous one
  ends and opens with its closing balance. Imports are serialized by locking
  the bank account.
- An identical retry returns the original statement; reuse of the reference
  with other details is rejected.
- Statements and lines are insert-only (model guards and database triggers).

CSV files need the columns `date` (YYYY-MM-DD), `amount`, `description` and
`reference`; the header fields are sent alongside the file.

## Matching

Each statement line can be matched with one posted ledger line on the bank
account that moves it by the same signed amount: customer payments and refunds
recorded with cash account 1000, Stripe payouts from the balance sync, and
bank-only items posted from the statement.

- **Automatic** (on import, and through `auto-match/` for all unmatched lines):
  candidates are unmatched ledger lines dated from 10 days before to 3 days
  after the bank date. With several candidates, the one whose entry memo or
  reference contains the bank reference wins, then the nearest date. A tie
  stays unmatched.
- **Manual**: match a line with a chosen ledger line, or unmatch it. Matches are
  never edited; unmatching deletes the match only.
- **Posted**: bank-only items (charges, interest) are posted against another
  account and matched in one step. Control accounts (1000, 1010, 1100, 2000)
  are refused; customer receipts go through payments.

A database trigger rejects any match whose ledger line is not posted, belongs to
another account or has a different amount.

## Report

`bank-reconciliation/?as_of=` compares the bank balance (first opening balance
plus lines booked up to that date) with the 1000 ledger balance:

- `unmatched_bank_lines`: on the statement only. Post or match them.
- `unmatched_ledger_lines`: dated from the first statement's start to `as_of`
  and not matched to a bank line booked by then: deposits in transit and
  outstanding payments, or errors.
- `opening_difference`: the first statement's opening balance against the
  ledger before its start date.
- `unexplained` = difference − opening difference − bank items + ledger items;
  non-zero means inconsistent data.
- `reconciled`: no difference and no reconciling items.

## APIs

All routes start with `/api/v1/accounting/`.

| Route | Method | Permissions |
| --- | --- | --- |
| `bank-statements/`, `bank-statements/<id>/` | GET | `view_bankstatement`, `view_bankstatementline`, `view_bankmatch` |
| `bank-statements/` | POST (JSON with `lines`) | `add_bankstatement`, `add_bankstatementline`, `add_bankmatch` |
| `bank-statements/import-csv/` | POST (multipart with `file`) | same as import |
| `bank-statements/auto-match/` | POST | `add_bankmatch` |
| `bank-lines/<id>/match/` | POST `{"journal_line"}` / DELETE | `add_bankmatch` / `delete_bankmatch` |
| `bank-lines/<id>/post/` | POST `{"account", "memo"}` | `add_bankmatch`, `add_journalentry` |
| `bank-reconciliation/?as_of=` | GET | read permissions and `view_journalline` |

Amounts are decimal strings. A new statement returns 201 and an identical retry
200; business-rule failures return 400 and database conflicts 409.

## Not supported

One bank account (1000), EUR only. Bank feeds (for example, open banking) and
formats other than CSV, such as CAMT.053 or MT940, are not imported.
