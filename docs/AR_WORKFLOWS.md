# Accounts receivable progress

The service layer supports draft invoices, posting, unpaid cancellation,
partial manual receipts and historical aging. Taxes and credit notes are not
yet supported. Use decimal strings or Decimal for amounts.

| Service | Accounting effect |
| --- | --- |
| `create_draft_invoice` | No ledger entry |
| `post_invoice` | Debit AR (1100), credit sales (4000) |
| `receive_invoice_payment` | Debit bank/cash (1000), credit AR (1100) |
| `cancel_invoice` | Unpaid posted invoice: debit sales, credit AR; link to original entry |
| `invoice_aging` | Read-only balances at the supplied accounting date |

Manual receipts are allocated to one invoice and use a unique reference. A
sequential replay with identical invoice, date and amount returns the original
receipt. Reuse with different details is rejected. Overpayments are rejected.
Cancellation of any invoice with receipts is rejected until a refund workflow
exists. Original posted journal entries remain in the ledger after reversal.

## Read APIs

All routes start with `/api/v1/accounting/`. GET only:

- `customers/`: requires `view_customer`.
- `invoices/`: requires `view_invoice` and `view_invoicereceipt`; filter by
  customer UUID, status or currency.
- `receipts/`: requires `view_invoicereceipt`; filter by invoice UUID.
- `ar-aging/?as_of=2026-01-31`: requires both invoice and receipt view permissions.

Permissions belong to the accounting app. Assign them through Django Groups.
List endpoints use page/page_size. Aging defaults to today's date and returns
all currency groups without pagination. It uses issue, due, receipt and
cancellation dates; future receipts and cancellations do not change historical
balances. On the due date an invoice is current, not overdue.

Invoice lists expose current open balances, not historical balances. Drafts
and cancelled invoices have zero current open balance. A paid invoice remains
posted with zero open balance.

## Remaining work

Goal 3 remains in progress: write APIs, credit notes/refunds, reusable payments
with multiple allocations, and hardened lifecycle/concurrency enforcement.
See [Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md). SQLite tests do not
establish PostgreSQL concurrency correctness. No Stripe integration exists yet.
