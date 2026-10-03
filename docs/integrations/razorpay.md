# Razorpay — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Razorpay page during this session.

## Source URLs (retrieved 2026-09-28)

- API fundamentals: https://razorpay.com/docs/api/understand/
- Subscriptions API: https://razorpay.com/docs/api/payments/subscriptions/
- Subscription webhook payloads: https://razorpay.com/docs/webhooks/payloads/subscriptions/
- Payments webhook payloads: https://razorpay.com/docs/webhooks/payloads/payments/
- Refund webhook payloads: https://razorpay.com/docs/webhooks/payloads/refunds/
- Dispute webhook payloads: https://razorpay.com/docs/webhooks/payloads/disputes/
- Recurring payments overview: https://razorpay.com/docs/api/payments/recurring-payments/
- Recurring UPI (AutoPay): https://razorpay.com/docs/api/payments/recurring-payments/upi/
- Recurring emandate: https://razorpay.com/docs/api/payments/recurring-payments/emandate/
- Recurring payments webhooks (token.*): https://razorpay.com/docs/api/payments/recurring-payments/webhooks/ (seen via official search result snippet only; page body not fetched)
- Refunds API: https://razorpay.com/docs/api/refunds/ and https://razorpay.com/docs/api/refunds/create-normal/
- Disputes API: https://razorpay.com/docs/api/disputes/
- Payment Links API: https://razorpay.com/docs/api/payments/payment-links/
- Settlements API: https://razorpay.com/docs/api/settlements/
- Webhooks overview: https://razorpay.com/docs/webhooks/
- Webhook validation: https://razorpay.com/docs/webhooks/validate-test/
- Webhook FAQs: https://razorpay.com/docs/webhooks/faqs/
- Subscription payment retries: https://razorpay.com/docs/payments/subscriptions/payment-retries/ (found in search, body not fetched)
- Official MCP server: https://github.com/razorpay/razorpay-mcp-server

## API version

- REST, JSON. Base URL `https://api.razorpay.com/v1/`; some endpoints live under `https://api.razorpay.com/v2`.
- No date-based version header. Versioning is path-based (v1/v2).

## Authentication

- HTTP Basic Auth with `KEY_ID:KEY_SECRET` (header `Authorization: Basic base64(KEY_ID:KEY_SECRET)`).
- Separate Test and Live key pairs (test keys are referenced in the docs; key prefix format such as `rzp_test_`/`rzp_live_` is **UNVERIFIED** in this session).
- Webhook secret is a separate, merchant-chosen value set per webhook in the Dashboard.

## Relevant endpoints (Nirantar scope)

### Plans and Subscriptions
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/plans` | Create plan |
| GET | `/v1/plans`, `/v1/plans/:id` | Fetch plans |
| POST | `/v1/subscriptions` | Create subscription |
| GET | `/v1/subscriptions`, `/v1/subscriptions/:id` | Fetch |
| PATCH | `/v1/subscriptions/:id` | Update |
| POST | `/v1/subscriptions/:id/cancel` | Cancel |
| POST | `/v1/subscriptions/:id/pause` | Pause |
| POST | `/v1/subscriptions/:id/resume` | Resume |
| GET | `/v1/subscriptions/:id/retrieve_scheduled_changes` | Pending update |
| POST | `/v1/subscriptions/:id/cancel_scheduled_changes` | Cancel pending update |
| DELETE | `/v1/subscriptions/:sub_id/:offer_id` | Remove linked offer |

Fetch-invoices-for-subscription and link-offer paths were rendered ambiguously by the docs page — exact paths **UNVERIFIED** (invoices are commonly fetched via `GET /v1/invoices?subscription_id=...`, **UNVERIFIED**).

### Recurring payments (tokens / UPI AutoPay / eNACH / cards)
Flow (official): create customer → create order (with `token` block) → authorization transaction via Checkout or registration link → token must reach `confirmed` → charge subsequent payments with the token.
- `POST /v1/customers`
- `POST /v1/orders` with `method` = `upi` / `emandate` / `card` / `nach` and a `token` object (`max_amount`, `expire_at`, `frequency` for UPI, `auth_type` for emandate: `netbanking` | `debitcard` | `aadhaar`; emandate order `amount` must be 0).
- `POST /v1/subscription_registration/auth_links` — registration link (no pre-created customer needed).
- `POST /v1/payments/create/recurring` — charge a confirmed token.
- Token fetch/cancel endpoints exist (exact paths **UNVERIFIED**; commonly `GET /v1/customers/:id/tokens`, `DELETE /v1/customers/:id/tokens/:token_id` — **UNVERIFIED**).
- Supported instruments: Emandate, Cards (min ₹1), Paper NACH (₹0), UPI (min ₹1), UPI OTM, UPI TPV. **India only; requires activation via Razorpay support.**
- UPI AutoPay `max_amount`: ₹1–₹99,999 for most categories; ₹1–₹2,00,000 for listed MCCs (6211, 6300, 7322, 6529, 5960). Frequencies: daily, weekly, fortnightly, bimonthly, monthly, quarterly, half_yearly, yearly, as_presented; `recurring_type` on/before/after.
- **NPCI: UPI Collect flow deprecated for new UPI AutoPay registrations effective 28 Feb 2026** — use UPI Intent.
- Emandate: default `max_amount` ₹9,99,900; default token expiry 40 years; order + authorization transaction must be created the same day before 23:59.
- Emandate/UPI mandate registration success can surface as an error response `BAD_REQUEST_ERROR` with `error_reason: "upi_dummy_payment"` — must not be treated as a failure.
- Pre-debit notification timing rules on Razorpay's side: **UNVERIFIED** (not captured from Razorpay pages).

### Payment Links
`POST /v1/payment_links`, `GET /v1/payment_links`, `GET /v1/payment_links/:id`, `PATCH /v1/payment_links/:id`, `POST /v1/payment_links/:id/cancel`, `POST /v1/payment_links/:id/notify_by/:medium`.

### Refunds
`POST /v1/payments/:id/refund` (normal or instant), `GET /v1/payments/:id/refunds`, `GET /v1/payments/:payment_id/refunds/:refund_id`, `GET /v1/refunds`, `GET /v1/refunds/:id`, `PATCH /v1/refunds/:id`. Only `captured` payments are refundable; uncaptured authorized payments auto-refund after 3 days.

### Disputes
`GET /v1/disputes`, `GET /v1/disputes/:id` (supports `expand[]=payment`, `expand[]=transaction.settlement`), `POST /v1/disputes/:id/accept`, `PATCH /v1/disputes/:id/contest`. Document-upload endpoint path **UNVERIFIED**.

### Settlements
`GET /v1/settlements`, `GET /v1/settlements/:id`, `GET /v1/settlements/recon/combined?year=yyyy&month=mm`.

## Webhook events

Signature verification (verified):
- Header: `X-Razorpay-Signature`
- Algorithm: HMAC-SHA256, key = webhook secret, message = **raw request body** ("Do not parse or cast the webhook request body"). Hex digest encoding is the widely used SDK behaviour but not explicitly quoted — **UNVERIFIED** wording.
- Dedup header: `x-razorpay-event-id` (unique per event).

Events (verified names):
- Subscriptions: `subscription.authenticated`, `subscription.activated`, `subscription.charged`, `subscription.completed`, `subscription.updated`, `subscription.pending`, `subscription.halted`, `subscription.cancelled`, `subscription.paused`, `subscription.resumed`
- Payments: `payment.authorized`, `payment.captured`, `payment.failed`; downtime: `payment.downtime.started`, `payment.downtime.updated`, `payment.downtime.resolved`; `order.paid`
- Refunds: `refund.created`, `refund.processed`, `refund.failed`, `refund.speed_changed`
- Disputes: `payment.dispute.created`, `payment.dispute.won`, `payment.dispute.lost`, `payment.dispute.closed`, `payment.dispute.under_review`, `payment.dispute.action_required`
- Tokens (from official search snippet; page body not fetched): `token.confirmed`, `token.rejected`, `token.cancelled`, `token.paused` (UPI only). Any `token.resumed` — **UNVERIFIED**.
- Payment links: `payment_link.paid` referenced; full list (`payment_link.partially_paid`, `payment_link.expired`, `payment_link.cancelled`) — **UNVERIFIED**.
- Settlement events (e.g., `settlement.processed`) — **UNVERIFIED**.

## Rate limits

- HTTP 429 on rate limiting; docs advise exponential/stepped backoff. **Numeric limits UNVERIFIED** (not published on the pages retrieved).
- **Observed live (2026-09-28, Nirantar sandbox test):** test mode caps payment links per account: `RATE_LIMIT_EXCEEDED: test mode limit of 30 reached for payment_link`. This is a quota, not a transient limit — retries will not help. Implication: demos/E2E in test mode must reuse links or use the mock provider for volume.
- Observed live: GET /v1/payments with Basic auth succeeds on the provided test key; GET of an unknown payment id returns a 4xx business error (mapped to ProviderRejected).

## Idempotency behavior

- No general idempotency-key header for Payments APIs found — **UNVERIFIED** that one exists.
- Refunds: the `receipt` parameter "is treated as an idempotency key" per payment; duplicate receipt returns a "Duplicate receipt found" error rather than a second refund.
- Webhooks: dedupe on `x-razorpay-event-id`.
- (RazorpayX payouts use a separate idempotency header; out of scope and not verified here.)

## Retry behavior (provider-side webhook retries)

- Retries at progressive intervals (exponential backoff) **for 24 hours**; after 24 hours of continuous failures the webhook is **disabled** and an alert email is sent; must be re-enabled manually in the Dashboard.
- Any non-2xx is a failure; if the server does not respond within **5 seconds** the delivery is treated as failed and resent.
- At-least-once delivery; **ordering not guaranteed** (e.g., `payment.captured` may arrive before `payment.authorized`).
- Webhook URLs must use port 80 or 443. Production requires TLS 1.2+.
- Subscription charge retries (provider-side dunning): `subscription.pending` → retries → `subscription.halted`. Exact retry count/schedule **UNVERIFIED** (see payment-retries page).

## Sandbox / test-mode behavior

- Test mode uses test API keys; test-mode webhooks fire from test transactions with the same payload shape as live.
- Default OTP **754081** when creating/editing/deleting a webhook in test mode.
- Test mandates / UPI AutoPay simulation specifics — **UNVERIFIED**.

## Error model

- HTTP codes: 400, 401, 404, 429 plus 5xx. Error body has an `error` object; `code` (e.g. `BAD_REQUEST_ERROR`), `description`, `reason` observed in docs. Full field list (`source`, `step`, `metadata`, `field`) — **UNVERIFIED** (errors reference page returned 404).

## Limitations

- Subscriptions available in India, Malaysia, Singapore; Recurring Payments API India-only and needs activation.
- UPI AutoPay mandate ceiling ₹99,999 (₹2,00,000 for specific MCCs). UPI Collect deprecated for new AutoPay registrations from 28 Feb 2026.
- Webhooks can be auto-disabled after 24h of failures — outage on Nirantar side can silently stop delivery.
- Event ordering not guaranteed.

## Security considerations

- Verify `X-Razorpay-Signature` with constant-time compare on the raw body before parsing.
- Whitelist Razorpay webhook IPs (list at "Razorpay IPs and Certificates" page — not retrieved).
- Store key secret and webhook secret in a secrets manager; never expose key secret to client (Checkout uses key_id only).
- MCP server: remote server at `https://mcp.razorpay.com/mcp` authenticates with base64 `KEY_ID:KEY_SECRET` — i.e., full API credentials. Treat as high-privilege.

## Official MCP server (github.com/razorpay/razorpay-mcp-server)

Remote URL `https://mcp.razorpay.com/mcp`; auth = base64 merchant token (key:secret). Tools marked "(remote: no)" are unavailable on the remote server. Read-only mode for remote — **UNVERIFIED**.
- Payments: `capture_payment`, `fetch_payment`, `fetch_payment_card_details`, `fetch_all_payments`, `update_payment`, `initiate_payment`, `resend_otp`, `submit_otp`
- Payment Links: `create_payment_link`, `create_payment_link_upi`, `fetch_all_payment_links`, `fetch_payment_link`, `send_payment_link`, `update_payment_link`
- Orders: `create_order`, `fetch_order`, `fetch_all_orders`, `update_order`, `fetch_order_payments`
- Refunds: `create_refund` (remote: no), `fetch_refund`, `fetch_all_refunds`, `update_refund`, `fetch_multiple_refunds_for_payment`, `fetch_specific_refund_for_payment`
- QR: `create_qr_code` (remote: no), `fetch_qr_code`, `fetch_all_qr_codes`, `fetch_qr_codes_by_customer_id`, `fetch_qr_codes_by_payment_id`, `fetch_payments_for_qr_code`, `close_qr_code` (remote: no)
- Settlements/Payouts: `fetch_all_settlements`, `fetch_settlement_with_id`, `fetch_settlement_recon_details`, `create_instant_settlement` (remote: no), `fetch_all_instant_settlements`, `fetch_instant_settlement_with_id`, `fetch_all_payouts`, `fetch_payout_by_id`
- Tokens: `fetch_tokens`, `revoke_token`, `create_registration_link` (remote: no)
- Helpers: `detect_stack`, `integrate_razorpay_checkout`

Note: no subscription/plan tools and no dispute tools were listed.

## Implementation notes for Nirantar

1. Webhook ingress: capture raw bytes, verify HMAC, persist (event id, raw body) to an inbox table keyed by `x-razorpay-event-id`, return 2xx in < 5 s, process asynchronously.
2. Treat state as eventually consistent; on any event, re-fetch the entity (subscription/payment/token) via API before transitioning Nirantar's state machine, since ordering is not guaranteed.
3. Model mandate lifecycle from `token.*` events; treat `upi_dummy_payment` as registration success.
4. Use refund `receipt` = Nirantar refund ID for idempotency; for other POSTs, implement client-side idempotency (look up by `receipt`/`notes` before retrying create).
5. Monitor webhook health: alert if no Razorpay events received for N minutes during business hours (auto-disable risk). Run a reconciliation poller (payments, subscriptions, settlements recon) as backstop.
6. Gate AI agents' MCP access: do not expose write tools (`capture_payment`, `create_refund`, `revoke_token`, `initiate_payment`) without human approval; prefer a Nirantar-side policy proxy.
7. Use UPI Intent (not Collect) for new AutoPay registrations.

## Backfill (history import) — added 2026-09-29 (P2, ADR-0012)

Sources retrieved 2026-09-29:
- Fetch all payments — `GET /v1/payments`: `from`, `to` (UNIX seconds), `count` (default 10, max 100), `skip`.
  Items include `invoice_id`, `error_code`, `error_reason`, `email`, `contact`, `vpa` (PII → hashed at landing).
  https://razorpay.com/docs/api/payments/fetch-all-payments/
- Fetch all subscriptions — `GET /v1/subscriptions`: `plan_id`, `from`, `to`, `count` (max 100), `skip`.
  https://razorpay.com/docs/api/payments/subscriptions/fetch-subscriptions/
- Fetch all customers — `GET /v1/customers`: `count` (max 100), `skip`; fields `name`, `email`, `contact`,
  `shipping_address` are personal data. https://razorpay.com/docs/api/customers/fetch-all/
- Invoices of a subscription — `GET /v1/invoices?subscription_id=…`: `payment_id`, `status`, `billing_start`,
  `billing_end`, `date`, `paid_at`, `amount`. Pagination parameters on this filter: **UNVERIFIED** (we send
  `count`/`skip`). https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices/

Observed live (2026-09-29, this project's test account):
- `payments`, `customers`, `invoices`, `payment_links` → 200; `subscriptions`, `plans` → **401 `{"error":
  "Unauthorized"}`** (error body is a plain string, not the usual error object — the adapter's error parser now
  handles both). Most likely the Subscriptions product is not activated on this test account (inference).
- The account has 0 payments, so a live backfill currently imports nothing.
