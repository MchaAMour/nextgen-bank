# Accounting, AR/AP, and Payments Plan

## Objective

Use this Django project to learn and build a reliable accounting system with:

- double-entry bookkeeping;
- accounts receivable (AR) and customer invoicing;
- accounts payable (AP) and supplier bills;
- Stripe-backed inbound payments;
- reconciliation and financial reporting.

Django/PostgreSQL will be the accounting system of record. Stripe will process
external payments, but it will not be the ledger.

## Non-negotiable accounting rules

1. Every posted financial event has a balanced journal entry: total debits must
   equal total credits.
2. Posted entries are immutable. Corrections are reversals or adjustments.
3. Use `Decimal` and ISO currency codes; never use floating-point amounts.
4. Derive accounting balances from posted journal lines. Cached balances are
   projections, not the source of truth.
5. Process Stripe payment success from a signed webhook, never from a browser
   success page.
6. Webhook processing is idempotent: receiving an event twice must not post two
   payments or two journal entries.

## Target flow

```text
Invoice or supplier bill
  -> balanced JournalEntry + JournalLines
  -> AR/AP open balance and aging reports

Customer pays invoice with Stripe
  -> Django creates Stripe Checkout Session or PaymentIntent
  -> Stripe sends signed webhook to Django
  -> Payment is recorded and allocated to invoice
  -> debit Stripe clearing / credit AR journal entry is posted
```

## Domain model

Start with one new Django app: `core_apps.accounting`.

### General ledger

- `ChartOfAccount`: code, name, type (`asset`, `liability`, `equity`,
  `income`, `expense`), normal balance, active flag.
- `JournalEntry`: UUID, entry date, memo, status (`draft`, `posted`,
  `reversed`), source reference, reversal reference, posting metadata.
- `JournalLine`: entry, account, debit, credit, currency, optional business
  references.

The central service will be `post_journal_entry(lines, source)`. It validates
that an entry is non-empty and balanced, then writes it in `transaction.atomic()`.

### Accounts receivable

- `Customer`
- `Invoice` and `InvoiceLine`
- `CreditNote`
- `CustomerPaymentAllocation`

Posting an invoice debits AR and credits revenue (and tax payable, if needed).
Receiving a payment debits cash/Stripe clearing and credits AR.

### Accounts payable

- `Supplier`
- `Bill` and `BillLine`
- `SupplierPaymentAllocation`

Posting a bill debits expense or asset and credits AP. Paying it debits AP and
credits cash/bank.

### Payments and Stripe

- `Payment`: direction, amount, currency, status, method, and provider ID.
- `PaymentAttempt`: Stripe PaymentIntent/Checkout Session reference and status.
- `StripeWebhookEvent`: unique Stripe event ID, payload, processing state, and
  error details.

Never store card numbers, CVC/CVV values, or Stripe secret keys in the database.

## Delivery goals and commits

Each goal is completed in its own focused commit.

## Test and commit policy

Track unresolved defects and improvement work in
[Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md). Implementation progress
does not imply those items are resolved. Large goals may use several focused
commits, each with its own verification evidence.

Every commit must be backed by tests appropriate to its scope. Before creating
the commit, run the relevant test module(s), a full affected-app test run, and
static validation such as `python manage.py check`. The commit message records
one completed goal only; the handoff reports the commands and their results.

Financial changes require tests for both the expected path and safety rules:
balance validation, duplicate/replayed requests or webhooks, partial payments,
permissions, and transaction rollback. No Stripe live-mode call will be made by
automated tests; Stripe tests use test-mode fixtures or mocks.

### Goal 0 — project foundation

- Add test settings and pytest/pytest-django.
- Add non-secret Stripe environment-variable placeholders.
- Define accounting roles and base currency.

**Commit:** `chore: add accounting development foundation`

**Verification:** `pytest`, `python manage.py check --settings=config.settings.test`

### Goal 1 — double-entry ledger

- Create the accounting app, migrations, chart of accounts, journal entry, and
  journal line models.
- Implement posting validation and starter chart seed data.
- Test balanced, unbalanced, empty, and already-posted entries.

**Commit:** `feat: add double-entry accounting ledger`

**Verification:** ledger model/service tests plus `python manage.py check`.

### Goal 2 — ledger APIs and reports

Implemented: read-only chart, entry list/detail, general-ledger and trial-balance
APIs with explicit Django model permissions. See [Ledger API](LEDGER_API.md)
for routes, group setup, report semantics, and current limitations.

- Add protected APIs for chart accounts, entries, trial balance, and general
  ledger.
- Add role permissions and pagination/filtering.

**Commit:** `feat: expose ledger reporting APIs`

**Verification:** API permission, filtering, pagination, and report-total tests.

### Goal 3 — AR invoicing

In progress: draft documents, invoice posting, unpaid cancellation via reversing
entries, partial manual receipts, historical aging, database integrity
enforcement, and protected read/write APIs implemented. Manual receipts
currently apply to one invoice. Reusable payments with allocation records,
refunds and credit notes remain. Totals round each line
to two decimal places before summing. Drafts do not create journal entries.

- Add customers, invoice drafts, invoice lines, posting, and cancellation.
- Calculate invoice open balances and AR aging.
- Support partial payment allocations.

**Commit:** `feat: add accounts receivable invoicing`

**Verification:** invoice posting, partial allocation, cancellation, and aging
tests.

### Goal 4 — Stripe invoice payments

- Add Stripe SDK configuration and a Checkout-first payment flow.
- Create one payment session only for an open invoice amount.
- Verify signed webhooks and record each Stripe event once.
- Allocate successful payments and post AR payment entries.

**Commit:** `feat: accept Stripe invoice payments`

**Verification:** signed webhook, duplicate webhook, failed payment, partial
payment, and balanced-ledger tests.

### Goal 5 — AP supplier bills

- Add suppliers, bills, lines, approvals, payment allocations, and AP aging.

**Commit:** `feat: add accounts payable workflow`

**Verification:** bill posting, approval, partial supplier payment, and AP-aging
tests.

### Goal 6 — reconciliation and financial reports

- Import bank/Stripe settlement data.
- Reconcile clearing account, Stripe fees, refunds, and payouts.
- Add balance sheet, profit and loss, customer/supplier statements, and audit
  history.

**Commit:** `feat: add reconciliation and financial reports`

**Verification:** payout, fee, refund, reconciliation, and financial-statement
tests.

## First vertical slice

Before expanding AR/AP, build and test this complete workflow:

1. Seed chart of accounts.
2. Create and post one customer invoice.
3. Show its AR balance and age.
4. Create a Stripe Checkout Session in test mode.
5. Receive the signed success webhook idempotently.
6. Allocate payment and post the balanced payment entry.
7. Test partial payment and webhook replay handling.

## Existing-code boundary

The current `BankAccount.account_balance` and transaction flows are a banking
demo and are not the accounting ledger. The new accounting app will be developed
alongside them without changing existing behaviour until a later, deliberate
integration step.
