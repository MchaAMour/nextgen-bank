# Stripe invoice payments

Customers pay posted invoices through Stripe-hosted Checkout (test mode while
developing). Django remains the ledger; Stripe only collects the money.

## Flow

1. A finance user calls `POST /api/v1/accounting/invoices/<id>/checkout/`
   (permission `add_stripecheckoutsession`). The response contains the Checkout
   `url` to send to the customer and the `amount`, which is the invoice's open
   balance at that moment.
2. The customer pays on Stripe. The success page is display-only; nothing is
   recorded from it.
3. Stripe sends a signed event to `POST /api/v1/accounting/stripe/webhook/`.
   The signature is the only authentication. When the session is paid, Django
   records a `CustomerPayment` (reference `stripe:<payment_intent>`) that debits
   Stripe clearing (1010) and credits AR (1100), then allocates it to the
   invoice up to the invoice's current open balance. Anything beyond that, for
   example because a manual payment arrived after checkout started, remains
   unapplied customer credit, so collected money is never dropped.

## Sessions

- One active session per invoice (database constraint). A new request reuses
  the active session while its amount still equals the open balance and it has
  more than 10 minutes left. Otherwise the old session is expired at Stripe
  first. A session that the customer already completed cannot be replaced;
  its webhook records the payment.
- Sessions are created with an idempotency key per invoice and attempt, tagged
  with `integration_identifier`, and without `payment_method_types`, so the
  payment methods enabled in the Stripe Dashboard apply.
- Amounts are sent in minor units. EUR, the Stripe account's settlement
  currency, is the only ledger currency (MONEY-01), so Stripe amounts match the
  ledger without conversion. No tax is calculated.

## Return pages

Checkout returns customers to two public pages, served by Django under
`/payments/`:

- `payments/success?session_id=...` shows the invoice, amount and status the
  webhook recorded. Until the webhook arrives it says the payment is being
  confirmed and reloads every 3 seconds. An unknown session is a 404.
- `payments/cancelled?invoice=...` (the invoice is added to
  `STRIPE_CHECKOUT_CANCEL_URL` when the session is created) says no payment
  was taken and links back to the invoice's open session while it is valid.

The pages only display state: payments are recorded by the webhook alone, so
reloading, closing or forging these URLs changes nothing. Session and invoice
IDs are unguessable, and the pages show nothing beyond the invoice number,
amount and status.

## Webhook events

| Event | Effect |
| --- | --- |
| `checkout.session.completed` | `payment_status` paid: record payment. Unpaid (delayed method): mark `awaiting_payment` |
| `checkout.session.async_payment_succeeded` | Record payment |
| `checkout.session.async_payment_failed` | Mark `failed`; nothing posted |
| `checkout.session.expired` | Mark `expired`; nothing posted |

Every verified event is stored once in `StripeWebhookEvent` with its payload,
status, attempt count and any error. Redelivery of a processed or ignored event
changes nothing, and a paid session is final, so the completed and
async-succeeded events for one session post one payment. Events for sessions
this application did not create, and other event types, are stored as
`ignored`. If processing raises, the work rolls back, the event is stored as
`failed` and the endpoint returns 500 so that Stripe retries. Invalid
signatures return 400 and store nothing; a missing signing secret returns 503.

## Configuration

Settings come from environment variables (never the database):
`STRIPE_SECRET_KEY`, `STRIPE_ACCOUNT_ID`, `STRIPE_WEBHOOK_SECRET`,
`STRIPE_CHECKOUT_SUCCESS_URL` and `STRIPE_CHECKOUT_CANCEL_URL`. Locally,
`local.yml` loads `../.env-stripe` and then `../.env-stripe-listen` from the
directory above the repository, so neither file can be committed.

Use a restricted key (`rk_`) as `STRIPE_SECRET_KEY` with Checkout Sessions:
Write and Read on Balance, Balance transaction sources, Charges, Payouts,
Disputes and Accounts. A missing permission makes Stripe name it in the error.
`make stripe-listen` needs Debugging Tools: Write, which the app key should not
have; put a separate key with only that permission in `../.env-stripe` as
`STRIPE_CLI_KEY`.

`STRIPE_ACCOUNT_ID` (`acct_...`) names the one Stripe account this ledger
reconciles. Before its first Stripe call, each process checks that the key
belongs to that account and refuses it otherwise (the API answers 503). After
changing keys, recreate every container that loads them (api, celeryworker,
flower).

### Local webhooks

```sh
make stripe-webhook-secret   # writes ../.env-stripe-listen with the `stripe listen` signing secret
make stripe-listen           # forwards the four events above to localhost:8080
```

Restart the `api` service after writing the secret. The CLI secret only
verifies events forwarded by `stripe listen`. A public deployment needs its own
Dashboard webhook endpoint and signing secret (STRIPE-03).

## Reconciliation

Payments enter Stripe clearing (1010) at their gross amount. The balance
transaction sync then records every movement of the Stripe balance once, by its
Stripe ID, so that 1010 follows the Stripe balance:

| Reporting category | Entry |
| --- | --- |
| `charge` | Matched to payment `stripe:<payment_intent>`; fee: Dr 5100 Payment Processing Fees / Cr 1010 |
| `refund` | Refund of the payment's unapplied credit: Dr 1100 / Cr 1010 |
| `payout` | Dr 1000 Cash and Bank / Cr 1010; payout fees Dr 5100 |
| `payout_reversal` | Dr 1010 / Cr 1000 |
| `fee` | Stripe fees billed separately: Dr 5100 / Cr 1010 |
| `dispute` | Disputed amount withdrawn: Dr 1150 Disputed Stripe Payments; dispute fee Dr 5100 / Cr 1010 |
| `dispute_reversal` | Dispute won: Dr 1010 / Cr 1150; a returned fee credits 5100 |

Disputes post whether or not the charge belongs to a recorded payment; the
payment is linked when it is. While a dispute is open, each sync looks up its
status in Stripe. Once Stripe reports it as lost, its amount is written off once
(Dr 5110 Chargeback Losses / Cr 1150) and linked as the transaction's
`loss_entry`. The invoice stays paid: the customer did pay, and billing them
again is a separate decision. The report's `open_disputes` is the 1150 balance,
money held by open disputes.

Nothing is posted, and the transaction is stored as `needs_review` with a
reason, when the charge's payment is not recorded yet (its webhook is pending),
the amounts or the clearing account do not match, a refund exceeds the
payment's unapplied credit, the currency is not EUR, or the category is not
listed. Every sync retries these. A refund of a payment
that is already allocated to an invoice stays in review until the allocation is
released (see AR workflows); the next sync then posts it and the invoice is
open again.

Each sync lists transactions created from one day before the newest stored one
(the first sync lists all), with their sources expanded.

| Route | Method | Permissions |
| --- | --- | --- |
| `stripe/balance-transactions/` | GET (filters `state`, `reporting_category`) | `view_stripebalancetransaction` |
| `stripe/balance-transactions/sync/` | POST | `add_stripebalancetransaction`, `add_journalentry` |
| `stripe/reconciliation/` | GET | `view_stripebalancetransaction`, `view_journalline` |

The reconciliation report compares the 1010 ledger balance with the Stripe EUR
balance (available plus pending). `difference` = Stripe − ledger; the net of
items in review explains part of it and the rest is `unexplained`.
`unsynced_payments` counts Stripe payments whose charge is not synced yet; sync
before reading the report. `reconciled` is true only with no difference, no
review items and no unsynced payments.

From the command line, `python manage.py sync_stripe_balance` syncs and prints
the report. `python manage.py schedule_stripe_sync` creates or updates the
Celery beat task "Stripe balance sync" (hourly by default; `--every-minutes N`
with N of at least 5, `--disable` to pause it). It is also editable under
Periodic tasks in the Django admin. Without `STRIPE_SECRET_KEY` the task logs a
warning and skips.

The Stripe account must be used only by this application, since every balance
transaction is reconciled against this ledger. See
[Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md) (STRIPE-02).
