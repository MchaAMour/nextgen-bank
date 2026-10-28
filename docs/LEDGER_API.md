# Ledger reporting API

All routes below are under `/api/v1/accounting/` and accept GET only
(plus standard HEAD/OPTIONS). Authenticate using the existing JWT mechanism.

| Route | Purpose | Filters |
| --- | --- | --- |
| `accounts/` | Chart of accounts | `account_type`, `is_active`, `search` |
| `entries/` | Entries including drafts, with lines | `status`, `entry_date`, `reference` |
| `entries/<uuid>/` | One entry with its lines | None |
| `general-ledger/` | Posted journal lines | `start_date`, `end_date`, `currency`, `account` (UUID) |
| `trial-balance/` | Net debit/credit balances per account and currency | Same as general ledger |

Lists use `page` and `page_size` (default 25, maximum 100). The trial balance
returns all matching account totals without pagination, so its totals cover the
entire filtered report. Monetary values are decimal strings.

## Access

Use Django admin's Groups and Permissions to create an Accountant group:

- Grant `accounting.view_chartofaccount` for chart access.
- Grant both `accounting.view_journalentry` and `accounting.view_journalline`
  for entry detail and financial reports.
- Assign the group to authorised finance users.

An active superuser also has access. Staff status alone grants no access.
Existing customer, teller, and branch-manager roles do not implicitly grant
accounting access. Group names are descriptive; the actual permissions control
access. No write permissions or editing endpoints are introduced here.

## Report meaning

Dates are inclusive and use `entry_date`, the accounting date. Omit start_date
to include all recorded history up to end_date. Supplying both dates reports
movement during that period; it is not a closing balance with brought-forward
amounts. Currency filters require three uppercase letters.

Draft entries are excluded. Reversed originals remain included because a
posted correcting entry must offset them. A reversal workflow is still to be
implemented. Accounts with no matching posted lines are omitted.

Trial balance groups currencies separately and calculates net debits or net
credits for each account. Its `balanced` flag compares those totals. A report
filtered to a single account can legitimately have `balanced: false`; that
does not imply that its underlying journal entries are unbalanced.

Example request:

```text
GET /api/v1/accounting/trial-balance/?end_date=2026-01-31&currency=EUR
```

## Development verification

Run `make test` and `make check`. The isolated container uses SQLite and does
not require Stripe keys or the development database. Before production use,
PostgreSQL-specific concurrency and decimal aggregation require integration
tests. Public journal QuerySet bulk writes are prohibited, including for drafts;
use validated instance operations. Save/delete guards consult persisted state,
including a line's original parent, to reject stale-object mutation of posted
history. Raw SQL and private ORM methods remain outside these guards. Database
enforcement and concurrent edit/post tests are still needed before write APIs.
