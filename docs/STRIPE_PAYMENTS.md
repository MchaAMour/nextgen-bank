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
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_CHECKOUT_SUCCESS_URL`
and `STRIPE_CHECKOUT_CANCEL_URL`. Locally, `local.yml` loads
`../.env-stripe` and then `../.env-stripe-listen` from the directory above the
repository, so neither file can be committed.

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

Nothing is posted, and the transaction is stored as `needs_review` with a
reason, when the charge's payment is not recorded yet (its webhook is pending),
the amounts or the clearing account do not match, a refund exceeds the
payment's unapplied credit, the currency is not EUR, or the category is not
listed (disputes, for example). Every sync retries these. A refund of a payment
that is already allocated to an invoice stays in review until allocations can
be released (AR-05).

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
the report. The Celery task `sync_stripe_balance_transactions` can be scheduled
under Periodic tasks in the Django admin (django_celery_beat); no schedule is
set up by default.

The Stripe account must be used only by this application, since every balance
transaction is reconciled against this ledger. See
[Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md) (STRIPE-02).
