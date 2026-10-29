# Accounts payable

Supplier bills are drafted, approved by a second person, then posted. Supplier
payments are recorded once and applied to bills, partially or in full. All
amounts are EUR; tax is not supported (MONEY-01).

| Service | Accounting effect |
| --- | --- |
| `create_draft_bill`, `replace_draft_bill`, `delete_draft_bill` | None; drafts only |
| `approve_bill` | None. The approver must be a signed-in user other than the bill's creator |
| `post_bill` | Debit each line's account (summed per account), credit AP (2000) |
| `cancel_bill` | Draft or approved: none. Posted, without allocations: an entry reversing the posting |
| `record_supplier_payment` | Debit AP, credit cash and bank (1000); optional allocations |
| `allocate_supplier_payment` | None; applies unapplied payment to a posted bill |
| `bill_aging` | Open bills by days past due at a date, less unapplied payments |
| `supplier_statement` | Opening balance, movements with running balance, closing balance |

## Bills

- `number` is the supplier's invoice number, unique per supplier.
- Each line names the account it is charged to: an active expense account, or
  an asset account for purchases that are capitalized. Control accounts (1000,
  1010, 1100, 2000) are refused.
- Approved bills cannot be edited; cancel and draft again. The approver and
  approval time are stored, and the database rejects a creator approving their
  own bill.
- A bill with allocated payments cannot be cancelled (allocation release is not
  supported, as in AR-05).

## Payments and balances

A supplier payment debits AP in full when made. Allocations are subledger
records without ledger effect; whatever is not allocated is a prepayment to the
supplier, which reduces AP.

- Bill open balance = posted total − allocations.
- Payment unapplied amount = amount − allocations.
- Aging `net` = open bills − unapplied payments = the AP (2000) credit balance
  at that date. The closing balances of all supplier statements add up to it
  too.

Payment and allocation references are unique; an identical retry returns the
original record and reuse with other details is rejected. Supplier payments
appear as credits to 1000, so bank statement lines match them automatically
([Bank reconciliation](BANK_RECONCILIATION.md)).

## Database enforcement

Migration `0012_accounts_payable` (SQLite and PostgreSQL):

- Check constraints keep lifecycle fields consistent with the status and stop
  self-approval.
- Bills are inserted as drafts and change only draft → approved → posted, or to
  cancelled. Posting needs a posted entry sourced from the bill crediting AP with
  its total; cancelling a posted bill needs a posted entry reversing it and no
  allocations. Only drafts can be deleted, and only their lines can change.
- Supplier payments and allocations are insert-only. A payment needs a posted
  entry debiting AP and crediting cash for its amount; an allocation needs a
  posted bill of the same supplier and enough open and unapplied balance.
  PostgreSQL locks the payment, then the bill.
- Bulk ORM writes raise `ValidationError`.

## APIs

All routes start with `/api/v1/accounting/`.

| Route | Method | Permission | Body / notes |
| --- | --- | --- | --- |
| `suppliers/`, `suppliers/<id>/` | GET / POST / PUT, PATCH | `view_supplier` / `add_supplier` / `change_supplier` | |
| `suppliers/<id>/statement/` | GET | `view_supplier`, `view_bill`, `view_supplierpayment` | `start_date`, `end_date` |
| `bills/`, `bills/<id>/` | GET | `view_bill`, `view_supplierpaymentallocation` | filters supplier, status, currency |
| `bills/` | POST | `add_bill` | `{"supplier", "number", "bill_date", "due_date", "lines": [{"account", "description", "quantity", "unit_price"}]}` |
| `bills/<id>/` | PUT / DELETE | `change_bill` / `delete_bill` | Drafts only |
| `bills/<id>/approve/` | POST | `approve_bill` | |
| `bills/<id>/post/` | POST | `post_bill` | |
| `bills/<id>/cancel/` | POST | `cancel_bill` | `{"cancellation_date"}` |
| `supplier-payments/`, `supplier-payments/<id>/` | GET | `view_supplierpayment`, `view_supplierpaymentallocation` | |
| `supplier-payments/` | POST | `add_supplierpayment`, `add_supplierpaymentallocation` | `{"supplier", "reference", "amount", "paid_on", "allocations": [{"bill", "amount", "reference"}]}` |
| `supplier-payments/<id>/allocations/` | POST | `add_supplierpaymentallocation` | `{"bill", "amount", "reference", "allocated_on"}` |
| `ap-aging/?as_of=` | GET | bill and payment view permissions | |

Status codes follow AR: 201 new, 200 identical retry, 400 business rule, 409
database conflict. Every change writes an audit event with the acting user.
