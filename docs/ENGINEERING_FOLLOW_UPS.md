# Engineering follow-ups

Update this file with each implementation stage. Passing SQLite tests do not
close PostgreSQL or production-integrity items. Close an item only with a
regression test or documented verification; record the evidence here.

| ID | Priority / status | Issue and completion criteria |
| --- | --- | --- |
| INT-01 | High / open | Direct status saves can bypass the journal posting service. Enforce valid lifecycle transitions and balanced, nonempty entries at the database boundary; test raw SQL and private ORM bypasses. |
| INT-02 | High / open | Draft line writes do not share the posting service's parent lock. Serialize line edits, deletes and posting; test simultaneous operations using independent PostgreSQL connections. |
| INT-03 | High / open | Database line constraints only enforce nonnegative amounts. Add exactly-one-positive-side and currency rules; validate preexisting data before applying constraints. |
| INT-04 | High / open | Account type changes can reinterpret historic reports. Restrict structural changes once an account has posted lines; test account deactivation and historical reporting. |
| PG-01 | High / open | Add isolated PostgreSQL test settings/container and CI job. Verify concurrent posting, edit/post races, rollback, decimal aggregation and later allocation races. SQLite cannot verify row locks. |
| REV-01 | High / open | Implement atomic reversals and cancellation via correcting entries. Reconcile reversal status with existing immutability guards; test repeated reversal and original-plus-reversal totals. |
| CFG-01 | Medium / open | Test settings import base settings, which can load production dotenv values and configure Cloudinary before overrides. Isolate configuration and block external networking in tests. |
| CFG-02 | Medium / open | Pytest dependencies exist in requirements/local.txt but not Pipfile.lock. Align supported dependency installation paths and lock transitive dependencies. |
| AUTH-01 | Medium / open | Finance permissions currently require manual Group configuration. Add an idempotent role setup command with least-privilege tests. |
| MONEY-01 | Medium / open | USD is a provisional example default; two decimal places are assumed. Confirm base currency and supported currency precision before payment integration. Tax/VAT requirements are undecided. |
| OBS-01 | Low / open | Existing Django timezone and ReportLab deprecation warnings remain. Identify and address their sources during dependency maintenance. |
| AR-01 | High / in progress | Invoice posting, unpaid cancellation, single-invoice manual receipts and historical aging services now exist. Protected APIs and reusable payments with multiple allocations remain. Goal 3 is not complete. |
| AR-02 | Medium / partly addressed | Draft creation now re-reads and locks the customer. PostgreSQL concurrent deactivation tests remain; SQLite tests only verify persisted inactive customers are rejected. |
| AR-03 | High / open | Invoice/receipt model guards can be bypassed by bulk ORM updates or raw SQL; draft line edits do not acquire the parent posting lock. Add database enforcement and concurrency tests before public financial write APIs. |
| AR-04 | Medium / open | Manual receipt reference is unique and sequential replay is idempotent. Concurrent cross-invoice reuse can raise IntegrityError; define retry/conflict handling and test with PostgreSQL. |
| AR-05 | Medium / open | Tax, refunds, credit notes, period closing, and payment-to-many-invoice allocations remain. Paid invoices currently cannot be cancelled. |

## Completed safeguards

- `e35b587`: rejects public bulk journal writes, stale posted-entry deletion,
  and line movement out of posted entries. Twelve regression tests added;
  full suite at that commit: 49 passed. This does not close INT-01 or INT-02.

## Scope checkpoints

- Ledger reporting is read-only; do not describe the ledger as production-ready.
- AR is being built in smaller commits: draft documents first, then posting,
  cancellation, payment allocation and aging. Goal 3 is incomplete until all
  those workflows and their tests pass.

## Latest verification

AR service stage: 78 SQLite tests passed, including 17 posting, payment,
cancellation and historical-aging tests.
No PostgreSQL concurrency validation has been performed. Draft currency support
is provisionally limited to USD, EUR and GBP, all with two decimal places;
this is not a decision about the eventual business's base currency or taxes.
