# Engineering follow-ups

Update this file with each implementation stage. Passing SQLite tests do not
close PostgreSQL or production-integrity items. Close an item only with a
regression test or documented verification; record the evidence here.

| ID | Priority / status | Issue and completion criteria |
| --- | --- | --- |
| INT-01 | High / closed | Migration 0013: entries are inserted as drafts, post only with two or more balanced lines, change after posting only to `reversed` with an existing reversal, and only drafts can be deleted. Raw-SQL tests in `test_ledger_integrity.py`. |
| INT-02 | High / closed | Line saves and deletes lock the parent entry like `post_journal_entry`; the PostgreSQL line trigger locks entry rows FOR SHARE. A two-connection test shows a raw line insert waiting for a concurrent posting (about 1 s) and then being rejected. |
| INT-03 | High / closed | Check constraint `accounting_line_one_side` (exactly one positive side); the currency rule came with MONEY-01. Migration 0013 refuses to run over existing violating or unbalanced rows. |
| INT-04 | High / closed | The code and type of an account with posted lines cannot change (model guard and trigger); renaming and deactivation stay allowed, and reports include inactive accounts. |
| PG-01 | High / closed | `make test-postgres` runs the whole suite against a disposable PostgreSQL 16 (`config/settings/test_postgres.py`, `docker/test-postgres.yml`), including the two-connection race tests. The GitHub Actions workflow runs the SQLite and PostgreSQL suites on pushes to main and pull requests; its first run on main (run 40852556967, 2026-10-29) passed. |
| REV-01 | High / closed | `reverse_journal_entry` posts the swapped lines and marks the original `reversed` in one transaction; repeated reversal is refused by the service and the trigger. Invoice and bill cancellations use it. Tests cover offsetting totals, a failed reversal leaving the original posted, and deactivated accounts. |
| CFG-01 | Medium / closed | `base.py` skips `.envs/.env.production` under `config.settings.test*`, and `core_apps/conftest.py` makes any non-loopback socket connection raise in tests (database drivers connect through libpq and are unaffected). Tests: `test_tests_cannot_reach_the_network`, `test_deployment_dotenv_is_not_loaded_in_tests`. |
| CFG-02 | Medium / closed | `requirements/` is the only dependency source and what the Docker images install; `requirements/constraints.txt` (from `make lock-requirements`) pins every transitive package to the versions the tests ran with, and both images build with it. The stale `Pipfile`/`Pipfile.lock` were removed. |
| AUTH-01 | Medium / closed | `python manage.py setup_accounting_roles` creates or resets seven groups (Accounting viewer, AR clerk, AR manager, AP clerk, AP approver, Reconciler, Controller) with exactly their permissions (`core_apps/accounting/roles.py`). Tests: idempotent reset, least privilege (no role can change or delete journal or audit rows; clerks cannot post or approve), and role checks through the API. |
| MONEY-01 | Medium / decided | Decided 2026-10-28: EUR, the Stripe account's settlement currency, is the only ledger currency (migration 0008 constrains invoices, payments and journal lines; system check `accounting.E001` rejects another `ACCOUNTING_BASE_CURRENCY`). Other currencies need exchange rates and FX gain/loss accounting. Tax/VAT requirements remain undecided. |
| OBS-01 | Low / closed | The USE_TZ warning came from `import cloudinary` in `base.py`, which read Django settings while they were half-loaded; Cloudinary is now configured in `CommonConfig.ready()` and Django setup raises no deprecation warnings. The remaining ReportLab 4.2.2 warning (its own code on Python 3.12) is filtered by message and module in `pytest.ini`; drop the filter when ReportLab is upgraded. |
| STRIPE-01 | Medium / open | `STRIPE_SECRET_KEY` is a full test-mode secret key. Before any shared or deployed environment, replace it with a restricted key (`rk_`) limited to Checkout Sessions (write), Balance, Balance Transactions and Disputes (read), and Account (read, for the `STRIPE_ACCOUNT_ID` check), and keep it in a secrets vault. |
| STRIPE-02 | High / closed | Balance transaction sync (migrations 0009 and 0016) posts charge fees, payouts, payout reversals, Stripe fees, refunds and disputes (withdrawal to 1150, reversal back, lost disputes written off to 5110), and reports clearing (1010) against the Stripe balance; `schedule_stripe_sync` schedules it in Celery beat. Other categories are left for review with nothing posted. Verified live against the dedicated sandbox on 2026-10-29 (evidence under Completed safeguards). |
| STRIPE-03 | Medium / open | Webhooks are only set up locally through `stripe listen`. A public deployment needs a Dashboard endpoint with its own signing secret, and an allowlist of Stripe webhook IP addresses. |
| STRIPE-04 | Medium / closed | End-to-end test-mode payment verified on 2026-10-28 (evidence under Completed safeguards). |
| STRIPE-05 | Medium / closed | A Celery worker started before a key change synced 21 transactions of the previous sandbox into the local ledger as review items (removed; nothing was posted). Every Stripe client now checks that its key belongs to `STRIPE_ACCOUNT_ID` and refuses any other account. |
| AR-01 | High / closed | Goal 3 scope delivered: drafts, posting, cancellation, reusable payments with multiple partial allocations, refunds, credit notes, historical aging reconciled to the AR ledger, database enforcement and protected APIs. Remaining AR items are tracked in AR-02, AR-04 and AR-05. |
| AR-06 | Medium / closed | Invoice and payment lists annotate balances with correlated subqueries (`with_balances()`). `test_invoice_list_balances_use_constant_queries` caps a five-invoice list at 8 queries. |
| AR-02 | Medium / closed | Draft creation re-reads and locks the customer. PostgreSQL two-connection test: a draft started during an uncommitted deactivation waits for it (about 1 s) and is then rejected. |
| AR-03 | High / closed | Closed by migration 0005: lifecycle check constraints, SQLite/PostgreSQL triggers for invoices, lines and receipts, rejected bulk AR writes, and parent-locked draft line writes. Evidence under Completed safeguards. Journal-line equivalents remain INT-01/INT-02. |
| AR-04 | Medium / closed | References and credit-note numbers are unique and identical retries replay. PostgreSQL two-connection test: two payments reusing one reference concurrently commit exactly one payment and one journal entry; the other request gets IntegrityError (409 in the API) or a mismatch error. |
| AR-05 | Medium / closed | Period closing (migration 0014): closes lock posting through a date in the services and the database, and year ends move profit to retained earnings. Allocation release (migration 0015): an allocation is taken back in full with no journal entry, so a paid invoice can be corrected or cancelled and an allocated Stripe payment refunded; the settlement triggers ignore released allocations. Supplier allocation release is not built. Tax is tracked in MONEY-01. |

## Completed safeguards

- `e35b587`: rejects public bulk journal writes, stale posted-entry deletion,
  and line movement out of posted entries. Twelve regression tests added;
  full suite at that commit: 49 passed. This does not close INT-01 or INT-02.

- AR-03 (migration `0005_ar_document_integrity`): raw SQL and private ORM
  writes cannot change posted or cancelled invoices, their lines or receipts;
  receipts need a posted invoice, open balance and matching AR credit. 30
  regression tests in `test_ar_integrity.py`; with the SQLite triggers removed,
  12 fail. One-off PostgreSQL 16 run (throwaway container, not CI): all 113
  accounting tests passed; the SQLite-settings assertion fails by design. A
  two-connection check showed a raw line insert waiting 2.0s for the posting
  lock, then rejected after posting.

- AR settlements (migration `0006_ar_settlements`): payments, allocations,
  refunds and credit notes are insert-only and checked against ledger entries
  and open/unapplied balances by triggers. With the settlement triggers
  removed, 9 raw-SQL tests in `test_ar_settlements.py` fail. Aging `net`
  matches the AR ledger balance on three dates in a mixed scenario. A migration
  test converts a legacy receipt into a one-allocation payment. One-off
  PostgreSQL 16 run (throwaway container, not CI): 175 accounting tests passed;
  0006 reversed and reapplied; two connections inserting raw allocations of 60
  against a 100 invoice serialized (2.0s wait) and the second was rejected.

- Stripe Checkout (migration `0007_stripe_checkout`): 19 tests in
  `test_stripe_payments.py` cover session creation, reuse and replacement,
  signed and forged webhooks, replays, delayed payment success and failure,
  expiry, overpayment credit, ignored and failed events with retry. A bug found
  while writing them: forged signatures were accepted in tests because
  verification went through the mocked API client; verification now uses
  `stripe.Webhook.construct_event`, which needs only the signing secret. One-off
  PostgreSQL 16 run: 194 accounting tests passed; 0006 and 0007 reversed and
  reapplied.

- STRIPE-04: on the local Docker stack, a real test-mode Checkout Session
  (125.50 USD, invoice STRIPE-E2E-1) was paid with card 4242 4242 4242 4242.
  `stripe listen` forwarded `checkout.session.completed`; the endpoint
  verified the signature and returned 200; the event was processed once on
  attempt 1; the session became paid; payment `stripe:<payment_intent>` was
  fully allocated; the entry was Dr 1010 / Cr 1100 125.50; the invoice open
  balance and the AR ledger balance were both 0.00 and the ledger balanced.

- MONEY-01 (migration `0008_eur_base_currency`): non-EUR invoices, payments
  and journal lines are rejected by model validation and by database check
  constraints (raw SQL test). The migration refuses to run while non-EUR rows
  exist (checked on PostgreSQL 16). On SQLite it suspends and restores all
  accounting triggers around the table rebuilds; the raw-SQL trigger tests
  still pass afterwards. 197 SQLite tests passed; one-off PostgreSQL run: 196
  accounting tests passed.

- Stripe reconciliation (migration `0009_stripe_balance_transactions`): 17
  tests in `test_stripe_reconciliation.py` with a mocked Stripe client cover
  fee posting and resync without duplicates, a charge synced before its
  webhook payment, amount and clearing-account mismatches, payouts with fees,
  payout reversals, separately billed Stripe fees, refunds from clearing and
  refunds beyond unapplied credit, unhandled categories and currencies, and the
  report (reconciled, explained by review items, unsynced payments) through the
  API with permissions, 502 and 503. 0009 reversed and reapplied on SQLite
  (single process) and PostgreSQL 16. Live run on 2026-10-29 against the
  dedicated sandbox: invoice RECON-1 (250.00 EUR) paid through Checkout, the
  webhook recorded the payment, and the sync posted the 8.13 EUR Stripe fee;
  Stripe clearing and the Stripe balance were both 241.87 EUR (reconciled).

- Bank statements (migration `0010_bank_statements`): 22 tests in
  `test_bank_reconciliation.py` cover import validation and chaining, replay,
  automatic matching (window, reference, nearest date, ties), manual
  match/unmatch, posting bank-only items, control-account refusal, timing
  differences across statements, opening differences, CSV parsing, raw-SQL
  rejection by the triggers, and the APIs. 0010 reversed and reapplied on
  SQLite and PostgreSQL 16; one-off PostgreSQL run: 235 accounting tests passed.

- Reports and audit (migration `0011_audit_events`): 8 tests in
  `test_financial_reports.py` cover a January scenario with invoices, a
  cancellation, a payment with customer credit, a refund, a credit note and bank
  charges: the balance sheet balances on two dates, profit and loss equals
  current earnings, customer statements match the AR ledger with running and
  opening balances, audit events are written once per change with the API user
  and reject raw updates and deletes. One-off PostgreSQL 16 run: 243 accounting
  tests passed; 0011 reversed and reapplied.

- AP (migration `0012_accounts_payable`): 13 tests in
  `test_accounts_payable.py` cover approval by a second user, posting by
  account, line-account rules, draft replace and delete, partial payments, a
  prepayment, aging on three dates equal to the AP ledger, supplier statements,
  cancellation with and without allocations, bank matching of supplier payments,
  raw-SQL rejection by the triggers (skipping approval, self-approval, posted
  changes, over-allocation, foreign payment entries) and the API with separate
  clerk and approver permissions and audit actors. 0012 reversed and reapplied
  on SQLite and PostgreSQL 16; one-off PostgreSQL run: 256 accounting tests
  passed.

- Ledger integrity (migration `0013_ledger_integrity`): 22 tests in
  `test_ledger_integrity.py`, two of them PostgreSQL-only race tests. With
  `make test-postgres` the full suite (279 tests, including the settings test,
  which now accepts the PostgreSQL test settings) passes; 0013 reversed and
  reapplied on both databases.

- Period closing (migration `0014_period_close`): 6 tests in
  `test_period_close.py` cover posting refused on and before a closed date
  (service and raw SQL), forward-only closes, drafts blocking a close,
  immutable closes, two year ends moving profit to retained earnings with the
  balance sheet balanced and profit and loss unchanged, and corrections in the
  next period. 289 SQLite and 293 PostgreSQL tests pass; 0014 reversed and
  reapplied on both.

## Scope checkpoints

- Ledger reporting is read-only; do not describe the ledger as production-ready.
- Goals 3 to 6 (AR, Stripe, AP, reconciliation, reports, audit) pass on
  SQLite and PostgreSQL in CI. Until the restricted key (STRIPE-01), a public
  webhook (STRIPE-03) and a tax decision (MONEY-01), none of it is
  production-ready.

## Latest verification

Checkout return pages stage (2026-10-29): `make test` 320 passed and 4
PostgreSQL-only tests skipped on SQLite; `make test-postgres` 324 passed on
PostgreSQL 16; `makemigrations --check` found no changes. Live sandbox
payment and balance sync reconciled (STRIPE-02); GitHub Actions passed on main
(PG-01). EUR is the only ledger currency (MONEY-01). Tax is not supported.
