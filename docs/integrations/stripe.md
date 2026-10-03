# Stripe — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Stripe page during this session.

## Source URLs (retrieved 2026-09-28)

- Webhooks (signature, retries, ordering): https://docs.stripe.com/webhooks
- Subscription webhooks & statuses: https://docs.stripe.com/billing/subscriptions/webhooks
- Idempotent requests: https://docs.stripe.com/api/idempotent_requests
- Rate limits: https://docs.stripe.com/rate-limits
- Errors: https://docs.stripe.com/api/errors
- Versioning: https://docs.stripe.com/api/versioning
- Smart Retries / automatic retries: https://docs.stripe.com/billing/revenue-recovery/smart-retries
- India recurring payments (RBI e-mandates): https://docs.stripe.com/india-recurring-payments
- UPI AutoPay: https://docs.stripe.com/payments/upi/upi-autopay
- Test clocks: https://docs.stripe.com/billing/testing/test-clocks and https://docs.stripe.com/billing/testing/test-clocks/api-advanced-usage
- India invite-only (support, via official search snippet): https://support.stripe.com/questions/stripe-accounts-are-invite-only-in-india

## API version

- Current version per versioning page: **`2026-08-26.dahlia`**. Monthly non-breaking releases; two major (breaking) releases per year.
- Override per request with `Stripe-Version` header; default = account's API version set in Workbench. Webhook payload shape follows the API version set on the endpoint (or account default).
- v2 APIs (e.g., `/v2/core/event_destinations`) use preview versions such as `2026-08-26.preview`.

## Authentication

- Secret key (`sk_test_...` / `sk_live_...`) via HTTP Basic (key as username) or `Authorization: Bearer <key>` (v2 examples). Restricted keys are recommended for least privilege (restricted key specifics **UNVERIFIED** in this session).
- Webhook signing secret per endpoint (`whsec_...`); different for test and live.

## Relevant endpoints (Nirantar scope)

Paths below are standard Stripe v1 resources referenced across the retrieved pages; per-endpoint reference pages were not individually fetched.
- Customers: `POST/GET /v1/customers` (supports `test_clock`)
- Products/Prices: `/v1/products`, `/v1/prices` (**UNVERIFIED** — not fetched)
- Subscriptions: `POST /v1/subscriptions`, `POST /v1/subscriptions/{id}` (update; `payment_behavior=pending_if_incomplete`, `proration_behavior`), resume `POST /v1/subscriptions/{id}/resume`, cancel `DELETE /v1/subscriptions/{id}` (cancel path **UNVERIFIED**)
- Subscription schedules: `/v1/subscription_schedules` (**UNVERIFIED** path)
- Invoices: `/v1/invoices`, finalize `POST /v1/invoices/{id}/finalize`; invoice payments list (`/v1/invoice_payments`, path **UNVERIFIED**)
- PaymentIntents / SetupIntents: `/v1/payment_intents`, `/v1/setup_intents` (with `payment_method_options[upi][mandate_options]`)
- Refunds `/v1/refunds`, Disputes `/v1/disputes` (**UNVERIFIED** — not fetched)
- Test clocks: `POST /v1/test_helpers/test_clocks`, `POST /v1/test_helpers/test_clocks/{id}/advance`, `GET`/`DELETE /v1/test_helpers/test_clocks/{id}`
- Event destinations (v2): `POST /v2/core/event_destinations`

## Webhook events

Signature verification (verified):
- Header `Stripe-Signature`: `t=<unix ts>,v1=<hex sig>[,v1=...][,v0=...]` (single line).
- Signed payload: `<timestamp>` + `.` + `<raw request body>`; HMAC-SHA256 keyed with endpoint secret; compare against every `v1` value with constant-time comparison; ignore non-`v1` schemes (v0 is a fake test signature).
- Official libraries default timestamp **tolerance 5 minutes**; never use tolerance 0 (disables the check).
- During secret rolling (up to 24 h overlap), multiple `v1` signatures are present.
- Retried deliveries get a new timestamp and signature.
- Raw body required; exempt route from CSRF; TLS 1.2/1.3; IP allowlist available (https://docs.stripe.com/ips).

Events relevant to Nirantar: `customer.subscription.created|updated|deleted|paused|resumed|trial_will_end`, `invoice.created|finalized|finalization_failed|paid|payment_failed|payment_action_required|upcoming|updated`, `payment_intent.created|succeeded` (plus `payment_intent.payment_failed`), `subscription_schedule.*` (aborted, canceled, completed, created, expiring, released, updated), `charge.refunded`, `charge.dispute.created`, `radar.early_fraud_warning.created`, `customer.updated`, `entitlements.active_entitlement_summary.updated`, `test_helpers.test_clock.advancing|ready`.

Subscription statuses: `trialing`, `active`, `incomplete` (23 h window), `incomplete_expired`, `past_due`, `canceled` (terminal), `unpaid`, `paused`.

## Rate limits

- Global: **100 req/s live, 25 req/s sandbox**; per-endpoint default 25 req/s.
- Subscriptions: 10 new invoices per subscription/min, 20/day, 200 quantity updates/subscription/hour. PaymentIntent: 1000 updates per object/hour. Search: 20 reads/s.
- Concurrency limits exist; 429 carries `Stripe-Rate-Limited-Reason` (`global-rate`, `endpoint-rate`, `global-concurrency`, `endpoint-concurrency`, `resource-specific`). 429 without that header may be `lock_timeout`.
- Read allocation: average ≤ 500 read requests per transaction over rolling 30 days (min 10,000/month).

## Idempotency behavior

- Header **`Idempotency-Key`** (≤ 255 chars, V4 UUID suggested; no PII). All `POST` accept it; no effect on GET/DELETE.
- Stores status code + body of first execution (including 500s) and replays it; keys may be pruned after ≥ 24 h.
- Parameter mismatch on reused key → `idempotency_error`; concurrent conflicting request → 409; validation failures are not saved (safe to retry).

## Retry behavior (provider-side webhook retries)

- Live: automatic retries for **up to 3 days with exponential backoff**. Sandbox: **3 retries over a few hours**. Email notification when an endpoint is failing.
- Manual resend: Dashboard up to 15 days, CLI up to 30 days.
- 3xx redirects are failures. Order not guaranteed; duplicates possible → dedupe on `event.id` (and `data.object.id` + `type`).
- Billing coupling: if `invoice.created` does not get a 2xx, automatic finalization of invoices is delayed up to 72 h — a Nirantar outage delays customer charges.

Payment retries (dunning): Smart Retries (AI-timed) — recommended default **8 tries within 2 weeks**, windows 1w/2w/3w/1m/2m; or custom schedule (up to 3 retries). After exhaustion: cancel / mark unpaid / leave past_due. **Stripe does NOT automatically retry payments on India-issued cards**, nor after hard declines.

## Sandbox / test-mode behavior

- Sandboxes with test keys; lower rate limits (don't load test against sandbox).
- Test clocks (Simulations): max 3 customers per clock, 3 subscriptions per customer, 10 standalone quotes; advance up to **two intervals** of the shortest subscription period at a time; clocks auto-deleted after **30 days**; list endpoints omit test-clock objects unless filtered; bank-debit collection not supported during advancement.
- India mandate test cards: `4000003560000123` (success), `...0297` (customer cancels/pauses after pre-debit), `...0248` (pre-debit notification not delivered), `...0263` (mandate cancelled); PM tokens `pm_card_indiaRecurring...`. In sandbox, off-session India PIs leave `processing` after ~15 min.
- Stripe CLI: `stripe listen --forward-to`, `stripe trigger <event>`.

## Error model

- Error object: `type` (`api_error`, `card_error`, `idempotency_error`, `invalid_request_error`), `code`, `decline_code`, `advice_code`, `network_advice_code`, `network_decline_code`, `message`, `param`, `doc_url`, `request_log_url`, `payment_intent`, `setup_intent`, `payment_method`, `payment_method_type`, `charge`, `source`.
- HTTP: 400, 401, 402, 403, 404, 409, 424, 429, 5xx.
- India mandate codes: `payment_intent_mandate_invalid`, `india_recurring_payment_mandate_canceled`, `processing_error`; decline `transaction_not_approved`.

## Limitations

- **Stripe accounts in India are invite-only** (new Indian businesses cannot self-sign-up; new Connect connected accounts invite-only). Critical feasibility risk for an India-first product.
- RBI e-mandates: cards charged **26 h** after payment request (pre-debit window), PI stuck in `processing` and **cannot be cancelled** during that window; UPI charges 1 day after notification.
- UPI AutoPay max **₹15,000** per auto-debit (default mandate `amount` 1500000 paise, `amount_type` maximum|fixed, default end 10 y, max 40 y). Cards > ₹15,000 need AFA per charge.
- Cannot pass an existing mandate to a Subscription; cannot cancel/update a mandate via API; Charges/Sources APIs can't create mandates; non-INR subs need an India PM attached before creation.
- No Smart Retries for India-issued cards.

## Security considerations

- Verify signatures with official SDK; enforce 5-min tolerance with NTP-synced clocks; restrict by Stripe IPs; roll `whsec_` periodically.
- Use restricted API keys per service; never log idempotency keys containing PII.

## Implementation notes for Nirantar

1. Treat Stripe as a secondary/international rail; do not rely on it for Indian merchants unless the merchant already holds an active Stripe India account.
2. Pin `Stripe-Version` in the adapter and on the webhook endpoint; both must match the SDK's pinned version.
3. Send `Idempotency-Key` = Nirantar command ID on every POST; map 409/`idempotency_error` explicitly.
4. Acknowledge `invoice.created` immediately (inbox pattern) to avoid 72 h finalization delays.
5. For India cards, Nirantar must own dunning (Stripe won't retry) and must model the 26 h `processing` window as a distinct state ("pre-debit pending").
6. Use test clocks in CI with a dedicated sandbox; clean up clocks after tests.
