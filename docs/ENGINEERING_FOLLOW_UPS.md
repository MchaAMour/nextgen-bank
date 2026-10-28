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
| MONEY-01 | Medium / decided | Decided 2026-10-28: EUR, the Stripe account's settlement currency, is the only ledger currency (migration 0008 constrains invoices, payments and journal lines; system check `accounting.E001` rejects another `ACCOUNTING_BASE_CURRENCY`). Other currencies need exchange rates and FX gain/loss accounting. Tax/VAT requirements remain undecided. |
| OBS-01 | Low / open | Existing Django timezone and ReportLab deprecation warnings remain. Identify and address their sources during dependency maintenance. |
| STRIPE-01 | Medium / open | `STRIPE_SECRET_KEY` is a full test-mode secret key. Before any shared or deployed environment, replace it with a restricted key (`rk_`) limited to Checkout Sessions (write) and keep it in a secrets vault. |
| STRIPE-02 | High / open | Stripe payments debit clearing (1010), which is not yet reconciled: Stripe fees, payouts, refunds and disputes are not recorded. Required before live mode (Goal 6). |
| STRIPE-03 | Medium / open | Webhooks are only set up locally through `stripe listen`. A public deployment needs a Dashboard endpoint with its own signing secret, and an allowlist of Stripe webhook IP addresses. |
| STRIPE-04 | Medium / closed | End-to-end test-mode payment verified on 2026-10-28 (evidence under Completed safeguards). |
| AR-01 | High / closed | Goal 3 scope delivered: drafts, posting, cancellation, reusable payments with multiple partial allocations, refunds, credit notes, historical aging reconciled to the AR ledger, database enforcement and protected APIs. Remaining AR items are tracked in AR-02, AR-04 and AR-05. |
| AR-06 | Medium / closed | Invoice and payment lists annotate balances with correlated subqueries (`with_balances()`). `test_invoice_list_balances_use_constant_queries` caps a five-invoice list at 8 queries. |
| AR-02 | Medium / partly addressed | Draft creation now re-reads and locks the customer. PostgreSQL concurrent deactivation tests remain; SQLite tests only verify persisted inactive customers are rejected. |
| AR-03 | High / closed | Closed by migration 0005: lifecycle check constraints, SQLite/PostgreSQL triggers for invoices, lines and receipts, rejected bulk AR writes, and parent-locked draft line writes. Evidence under Completed safeguards. Journal-line equivalents remain INT-01/INT-02. |
| AR-04 | Medium / partly addressed | Payment, allocation and refund references and credit-note numbers are unique; identical sequential retries return the original record. The APIs map IntegrityError from concurrent reuse to 409 for client retry (mocked test). A PostgreSQL concurrent-reuse test remains. |
| AR-05 | Medium / open | Tax (MONEY-01) and period closing remain. Invoices with allocations or credit notes cannot be cancelled, and allocations cannot be released, so a paid invoice cannot be corrected and refunded yet. Refunds, credit notes and payment-to-many-invoice allocations are done. |

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

## Scope checkpoints

- Ledger reporting is read-only; do not describe the ledger as production-ready.
- Goal 3 (AR) workflows and their tests pass on SQLite and in a one-off
  PostgreSQL run. Without a PostgreSQL CI job (PG-01) and role setup
  (AUTH-01), AR is not production-ready either.

## Latest verification

Stripe Checkout stage (Goal 4): `make test` ran 195 SQLite tests, all passed;
`make check` reported no issues. A one-off PostgreSQL 16 run passed all 194
accounting tests (the SQLite-settings assertion fails by design). One live
test-mode Checkout payment succeeded end to end (STRIPE-04). PG-01 (a PostgreSQL test job)
remains open. Finance roles still need manual Group setup (AUTH-01).
EUR is the only ledger currency (MONEY-01). Tax is not supported.
