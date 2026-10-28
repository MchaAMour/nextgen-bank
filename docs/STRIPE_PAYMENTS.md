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

## Not yet handled

Stripe fees, payouts, refunds and disputes, and reconciliation of the clearing
account, are Goal 6. See [Engineering follow-ups](ENGINEERING_FOLLOW_UPS.md).
