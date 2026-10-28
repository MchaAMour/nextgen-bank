# Accounts receivable

The service layer supports draft invoices, posting, unpaid cancellation,
reusable customer payments with allocations, refunds of unapplied credit,
credit notes and historical aging. Tax is not supported (see MONEY-01). Use
decimal strings or Decimal for amounts.

| Service | Accounting effect |
| --- | --- |
| `create_draft_invoice` | No ledger entry |
| `replace_draft_invoice` | No ledger entry; replaces a draft's header and lines |
| `delete_draft_invoice` | No ledger entry; drafts only |
| `post_invoice` | Debit AR (1100), credit sales (4000) |
| `cancel_invoice` | Posted invoice without settlements: debit sales, credit AR; links the original entry |
| `record_customer_payment` | Debit cash (1000), credit AR (1100) for the full amount; optional allocations |
| `allocate_payment` | No ledger entry; applies unapplied payment credit to an invoice |
| `receive_invoice_payment` | Shortcut: one payment fully allocated to one invoice |
| `refund_payment` | Debit AR (1100), credit cash (1000); unapplied credit only |
| `issue_credit_note` | Debit sales (4000), credit AR (1100); up to the invoice's open balance |
| `invoice_aging` | Read-only balances at the supplied accounting date |

## Settlement model

A payment credits AR in full when it is received. Allocations are subledger
records: they apply part of a payment to an invoice of the same customer and
currency, without a journal entry. One payment can settle several invoices and
one invoice can be settled by several payments, partially or in full. Any
amount not allocated or refunded remains customer credit inside AR.

- Invoice open balance = posted total − allocations − credit notes.
- Payment unapplied amount = amount − allocations − refunds.
- Neither can go below zero; allocations and credit notes cannot precede the
  invoice date, and allocations and refunds cannot precede the payment date.

Payments, allocations and refunds have unique references; credit notes have
unique numbers. A retry with identical details returns the original record.
For a payment this includes its requested allocations. Reuse with different
details is rejected. Settlement records are append-only; corrections are new
records.

An invoice with allocations or credit notes cannot be cancelled. Issue a credit
note for its open balance instead. Correcting an invoice that is already paid
needs allocation release, which is not supported yet (see AR-05).

Existing manual receipts were migrated to payments with one allocation each,
keeping their reference and journal entry. The `receipts/` endpoint was
replaced by `payments/`.

## Database enforcement

Migrations `0005_ar_document_integrity` and `0006_ar_settlements` back the
model guards with the database (SQLite and PostgreSQL), so raw SQL and private
ORM writes are also rejected:

- Check constraints: due date not before issue date, supported currency, and
  lifecycle fields consistent with status (a posted invoice has its journal
  entry and a positive total; only a cancelled posted invoice has a cancellation
  entry).
- Invoices are inserted as drafts. Posting requires a posted journal entry
  sourced from that invoice. A posted invoice may only become cancelled with a
  posted entry that reverses its own, and only without allocations or credit
  notes. Non-draft invoices cannot be deleted.
- Lines of non-draft invoices cannot be inserted, updated, moved or deleted.
- Payments, allocations, refunds and credit notes are insert-only. Payments,
  refunds and credit notes require a posted entry whose debit and credit legs
  use the accounts above and equal the record's amount. Allocations and credit
  notes require enough open invoice balance; allocations and refunds require
  enough unapplied payment.
- Bulk ORM writes on these models raise `ValidationError`.

Draft line writes lock the parent invoice row like `post_invoice`. On
PostgreSQL the triggers also lock parent rows (payment before invoice, as the
services do), so concurrent writes cannot overpay or over-credit. Violations
raise `IntegrityError`.

## APIs

All routes start with `/api/v1/accounting/`.

### Read

| Route | Permissions | Filters |
| --- | --- | --- |
| `customers/`, `customers/<id>/` | `view_customer` | |
| `invoices/`, `invoices/<id>/` | `view_invoice`, `view_paymentallocation`, `view_creditnote` | customer, status, currency |
| `payments/`, `payments/<id>/` | `view_customerpayment`, `view_paymentallocation`, `view_paymentrefund` | customer, currency |
| `credit-notes/` | `view_creditnote` | invoice |
| `ar-aging/?as_of=2026-01-31` | invoice and payment view permissions | |

Permissions belong to the accounting app. Assign them through Django Groups.
List endpoints use page/page_size. Invoice detail includes lines; payments
include allocations, refunds and `unapplied_amount`. Invoice lists compute
balances in one query (no per-invoice queries).

Invoice and payment balances are current. Aging is historical: it uses issue,
due, allocation, credit-note, refund and cancellation dates, so later records
do not change earlier balances. On the due date an invoice is current, not
overdue. Each currency group has buckets (`current`, `1_30`, `31_60`,
`61_90`, `over_90`), `total` open invoices, `unapplied` customer credit and
`net` = total − unapplied, which equals the AR (1100) ledger balance at that
date.

### Write

Every write goes through the services above and needs its own permission:

| Route | Method | Permission | Body / effect |
| --- | --- | --- | --- |
| `customers/` | POST | `add_customer` | Create a customer |
| `customers/<id>/` | PUT, PATCH | `change_customer` | Update name, email or active flag |
| `invoices/` | POST | `add_invoice` | Create a draft with its lines |
| `invoices/<id>/` | PUT | `change_invoice` | Replace a draft's header and all lines |
| `invoices/<id>/` | DELETE | `delete_invoice` | Delete a draft |
| `invoices/<id>/post/` | POST | `post_invoice` | Post a draft |
| `invoices/<id>/cancel/` | POST | `cancel_invoice` | `{"cancellation_date"}`; no settlements |
| `payments/` | POST | `add_customerpayment`, `add_paymentallocation` | `{"customer", "reference", "amount", "currency", "received_on", "allocations": [{"invoice", "amount", "reference"}]}` |
| `payments/<id>/allocations/` | POST | `add_paymentallocation` | `{"invoice", "amount", "reference", "allocated_on"}` |
| `payments/<id>/refunds/` | POST | `add_paymentrefund` | `{"reference", "amount", "refunded_on"}` |
| `credit-notes/` | POST | `add_creditnote` | `{"number", "invoice", "amount", "issued_on", "reason"}` |

Send amounts (`quantity`, `unit_price`, `amount`) as decimal strings; JSON
numbers are rejected. Business-rule failures (non-draft edits, overpayment,
over-allocation, inactive customer, reference reuse with different details)
return 400. A new record returns 201; an identical retry returns 200 with the
original record. Database conflicts such as concurrent reuse of a reference
return 409 and can be retried. Invoice PATCH is not supported; PUT sends the
complete draft. Posting, payments, refunds and credit notes record the
requesting user as `posted_by` on their journal entry.

## Not yet supported

Tax, period closing, and releasing allocations to correct a paid invoice. See
[Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md). Stripe payments are Goal 4.
