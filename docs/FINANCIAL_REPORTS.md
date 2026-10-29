# Financial reports and audit history

Reports read posted ledger entries (and reversed ones, which their correcting
entries offset) by accounting date. All amounts are EUR decimal strings. Routes
start with `/api/v1/accounting/`.

| Route | Query | Permissions |
| --- | --- | --- |
| `balance-sheet/` | `as_of` (default today) | `view_journalentry`, `view_journalline`, `view_chartofaccount` |
| `profit-and-loss/` | `start_date`, `end_date` (both required) | same |
| `customers/<id>/statement/` | `start_date`, `end_date` | `view_customer`, `view_invoice`, `view_customerpayment`, `view_paymentrefund`, `view_creditnote` |
| `audit-events/` | `action`, `object_type`, `object_id`, `actor`, `since`, `until` | `view_auditevent` |
| `trial-balance/`, `general-ledger/` | see [Ledger API](LEDGER_API.md) | |

## Balance sheet

Assets, liabilities and equity with each account's balance in its normal
direction (accounts with a zero balance are left out). There is no period
closing yet, so profit since the first entry appears in equity as
`current_earnings`. `balanced` checks assets = liabilities + equity.

## Profit and loss

Income and expense movements between the two dates, inclusive, and
`net_income`. Over all dates it equals the balance sheet's current earnings.

## Customer statement

What a customer owes: the opening balance before `start_date`, every dated
movement in the period with a running balance, and the closing balance.
Invoices and refunds increase it; payments, credit notes and cancellations
reduce it. A payment counts in full when received, as in the ledger, so a
negative balance is credit in the customer's favour. The closing balances of
all customers add up to the AR (1100) ledger balance.

## Audit history

The services write an `AuditEvent` in the same transaction as each change:
customers created or updated, drafts created, replaced or deleted, invoices
posted or cancelled, every journal entry posted, payments, allocations, refunds,
credit notes, bank statement imports, bank line matches and unmatches, and
Stripe balance transactions posted. Each event has the action, the object type
and ID, a summary, details and the acting user (none for webhooks and other
system actions). Identical retries record nothing.

Audit events cannot be updated or deleted, also through raw SQL (migration
`0011_audit_events`). A user with audit history cannot be deleted; deactivate
the user instead. Stripe webhook deliveries are kept separately in
`StripeWebhookEvent`.

Not recorded: changes made outside the services, such as through the Django
admin or the shell.
