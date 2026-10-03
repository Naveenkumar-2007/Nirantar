# Cashfree Payments — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Cashfree page during this session.

## Source URLs (retrieved 2026-09-28)

- Subscription APIs overview (latest): https://www.cashfree.com/docs/api-reference/payments/latest/subscription/overview
- Create Subscription: https://www.cashfree.com/docs/api-reference/payments/latest/subscription/mandate/create
- Subscription webhooks (latest): https://www.cashfree.com/docs/api-reference/payments/latest/subscription/webhooks
- Webhooks overview (signature, retries, idempotency): https://www.cashfree.com/docs/payments/online/webhooks/overview
- Rate limits: https://www.cashfree.com/docs/api-reference/payments/rate-limits
- Payment Links webhooks: https://www.cashfree.com/docs/api-reference/payments/latest/payment-links/webhooks
- Create Payment Link: https://www.cashfree.com/docs/api-reference/payments/latest/payment-links/create (via official search snippet)
- Create Refund: https://www.cashfree.com/docs/api-reference/payments/latest/refunds/create (via official search snippet)
- Dispute webhooks: https://www.cashfree.com/docs/api-reference/payments/latest/disputes/dispute-webhooks
- Dispute evidence: https://www.cashfree.com/docs/api-reference/payments/latest/disputes/submit-evidence-to-contest-the-dispute-by-dispute-id (via official search snippet)
- Official MCP server: https://github.com/cashfree/cashfree-mcp

## API version

- Header **`x-api-version`**, date-based. Create Subscription page shows default **`2026-01-01`**; subscription overview labels the latest as "v6 (2026-01-01)". Older versions `2025-01-01` and `2023-08-01` still documented.
- Webhook payload versions are configured separately (e.g., `2023-08-01`, `2025-01-01`, `2026-01-01`); merchants on 2025-01-01 webhooks receive that version even for older subscriptions.

## Authentication

- Headers: `x-client-id` (App ID) and `x-client-secret` (Secret key) from Merchant Dashboard, plus `x-api-version`.
- Optional `x-request-id` (UUID for support tracing).
- Base URLs: Sandbox `https://sandbox.cashfree.com/pg`, Production `https://api.cashfree.com/pg`.

## Relevant endpoints (Nirantar scope)

### Subscriptions (base `/pg`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/subscriptions/plans` | Create plan |
| GET | `/subscriptions/plans/{plan_id}` | Fetch plan |
| POST | `/subscriptions` | Create subscription (mandate) — body requires `subscription_id` (1–250 chars), `customer_details.customer_email`, `customer_details.customer_phone`, `plan_details.plan_type` = `PERIODIC` or `ON_DEMAND` |
| GET | `/subscriptions/{subscription_id}` | Fetch subscription |
| PUT | `/subscriptions/{subscription_id}` | Manage (activate / pause / cancel / change) |
| POST | `/subscriptions/{subscription_id}/file-upload` | Upload physical NACH form |
| GET | `/subscriptions/payment-methods` | Eligible payment methods |
| GET | `/subscriptions/{subscription_id}/transaction-summary` | Transaction summary |
| POST | `/subscriptions/{subscription_id}/payments` | Raise a charge or create an auth |
| GET/PUT | `/subscriptions/{subscription_id}/payments/{payment_id}` | Fetch / manage (cancel/retry) payment |
| GET | `/subscriptions/{subscription_id}/payments` | List payments |
| POST/GET | `/subscriptions/{subscription_id}/controlled-notifications[/{id}]` | Merchant-controlled pre-debit notification |
| POST/GET | `/subscriptions/{subscription_id}/controlled-executions[/{id}]` | Merchant-controlled execution |
| POST | `/subscriptions/{subscription_id}/payments/{payment_id}/refunds` | Refund subscription payment |
| GET | `/subscriptions/{subscription_id}/payments/{payment_id}/refunds/{refund_id}` | Fetch refund |

Supported mandate types (overview page): e-Mandate (bank account, up to ₹1,00,00,000), Physical NACH, UPI AutoPay (page states "₹15,000–₹1,00,000 limits" — interpretation of that range, e.g. category-dependent ceilings, is **UNVERIFIED**), Cards SI (Indian and international).

### Payment links, refunds, disputes (non-subscription PG)
- `POST /pg/links` create payment link (URL in `link_url`); fetch/cancel/orders-for-link exist (paths **UNVERIFIED**, likely `GET /pg/links/{link_id}`, `POST /pg/links/{link_id}/cancel`, `GET /pg/links/{link_id}/orders`).
- `POST /pg/orders/{order_id}/refunds` — `refund_amount`, `refund_id` (merchant unique), `refund_note`, `refund_speed` (default `STANDARD`). Refunds allowed only within **6 months** of the transaction.
- Disputes: get by order ID / payment ID / dispute ID, accept by dispute ID, submit evidence by dispute ID (files jpeg/jpg/png/pdf ≤ 20 MB). Exact paths **UNVERIFIED**.
- Settlements: get all settlements, settlements by order ID (paths **UNVERIFIED**).

## Webhook events

Signature verification (verified):
- Headers: `x-webhook-timestamp`, `x-webhook-signature`.
- Signed message: **timestamp concatenated with the raw request body** (no separator stated).
- Algorithm: HMAC-SHA256 keyed with the merchant's **PG client secret**, output **Base64-encoded**; compare with `x-webhook-signature`.
- Must use raw payload, not re-serialized JSON.
- Timestamp tolerance: none specified by Cashfree — **UNVERIFIED** (Nirantar should enforce its own).

Subscription events (latest): `SUBSCRIPTION_STATUS_CHANGED` (ACTIVE, ON_HOLD, COMPLETED, CUSTOMER_CANCELLED, CUSTOMER_PAUSED, EXPIRED, LINK_EXPIRED, BANK_APPROVAL_PENDING, CANCELLED, CARD_EXPIRED), `SUBSCRIPTION_AUTH_STATUS`, `SUBSCRIPTION_PAYMENT_NOTIFICATION_INITIATED`, `SUBSCRIPTION_PAYMENT_SUCCESS`, `SUBSCRIPTION_PAYMENT_FAILED`, `SUBSCRIPTION_PAYMENT_CANCELLED`, `SUBSCRIPTION_REFUND_STATUS`, `SUBSCRIPTION_CARD_EXPIRY_REMINDER` (7 days before expiry), `SUBSCRIPTION_CONTROLLED_NOTIFICATION_STATUS`, `SUBSCRIPTION_CONTROLLED_EXECUTION_STATUS`.
- Subscription webhooks are configured in a **separate Subscriptions tab** (Payment Gateway > Developers > Webhooks).
- Cashfree does **not** support replaying/resending subscription webhooks (e.g., `SUBSCRIPTION_PAYMENT_SUCCESS`) — per official search snippet from FAQ.

Payment link: single type `PAYMENT_LINK_EVENT` (partial/complete payment, cancelled, expired; payload `version` 1).

Disputes: `DISPUTE_CREATED`, `DISPUTE_UPDATED`, `DISPUTE_CLOSED`.

PG payment events (`PAYMENT_SUCCESS_WEBHOOK`, `PAYMENT_FAILED_WEBHOOK`, `REFUND_STATUS_WEBHOOK` etc.) — names **UNVERIFIED** in this session.

## Rate limits

- Per-minute, by account (`app_id`) or IP. Production examples: Create Order 200/min, Get Order 400/min, Get Payments 100/min, Get Payment by ID 130/min, Pay Order 100/min (IP), Get Settlements 30/min, Initiate Refund 100/min, Get Refund 30/min.
- Headers: `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-retry` (seconds), `x-ratelimit-type` (`app_id`|`ip`). 429 on excess. Increases via dashboard (1–2 business days).
- Subscription-API-specific limits — **UNVERIFIED**.

## Idempotency behavior

- Request header **`x-idempotency-key`** (optional UUID) on Create Subscription — safe retry with same key; **HTTP 422** signals an idempotency conflict. Support on other endpoints — **UNVERIFIED** per endpoint. Key retention window — **UNVERIFIED**.
- Webhook dedupe: header **`x-idempotency-header`** (hashed, unique per payload) on webhooks version 2025-01-01+ (per webhooks overview; header name as published).
- Refund `refund_id` is merchant-supplied unique ID (natural idempotency; duplicate behaviour **UNVERIFIED**).

## Retry behavior (provider-side webhook retries)

- Configurable in Dashboard: **Default = 3 retries at 2, 10 and 30 minutes**; Fixed (≤10 retries, fixed interval); Exponential (retries, interval, multiplier); Custom (≤10 custom intervals).
- Retries until **HTTP 200** (docs say 200 specifically; treat other 2xx as risky).
- At-least-once delivery; duplicates possible. Delivery timeout — **UNVERIFIED**.
- Subscription charge retry (dunning) behaviour on provider side — **UNVERIFIED**.

## Sandbox / test-mode behavior

- Sandbox base URL `https://sandbox.cashfree.com/pg`; separate sandbox credentials.
- `simulate-payment` / `fetch-simulation` exist (seen as MCP tools) for simulating payments in sandbox. Subscription/mandate simulation specifics — **UNVERIFIED**.

## Error model

- HTTP 400 (invalid request), 401 (auth), 404, 422 (idempotency conflict), 429, 500. Body fields (commonly `message`, `code`, `type`) — **UNVERIFIED**.

## Limitations

- Subscription webhooks cannot be replayed → Nirantar must poll to recover missed events.
- Default webhook retry window is short (~42 minutes total) unless reconfigured.
- Refunds only within 6 months.
- Timestamps stored in IST; send ISO 8601.

## Security considerations

- Verify `x-webhook-signature` before parsing; enforce a Nirantar-side timestamp freshness window (e.g., 5 min) using `x-webhook-timestamp` for replay protection.
- The webhook signing key is the API client secret itself — rotating the secret affects both API auth and webhook verification; plan coordinated rotation.
- MCP server needs `PAYMENTS_APP_ID`/`PAYMENTS_APP_SECRET` (and payouts creds if enabled) — restrict `TOOLS=pg` and keep `ENV=sandbox` for development.

## Official MCP server (github.com/cashfree/cashfree-mcp)

Config: `ENV` = `sandbox` (default) | `production`; `TOOLS` = comma list of `pg`, `payouts`, `secureid`; `ELICITATION_ENABLED`.
- PG: `search` (docs), `get-input-source-help`, `create-payment-link`, `fetch-payment-link-details`, `cancel-payment-link`, `get-orders-for-a-payment-link`, `create-order`, `get-order`, `get-order-extended`, `get-eligible-payment-methods`, `get-payments-for-an-order`, `get-payment-by-id`, `create-refund`, `get-all-refunds-for-an-order`, `get-refund`, `get-all-settlements`, `get-split-and-settlement-details-by-order-id-v2-0`, `get-settlements-by-order-id`, `get-disputes-by-order-id`, `get-disputes-by-payment-id`, `get-disputes-by-dispute-id`, `accept-dispute-by-dispute-id`, `submit-evidence-to-contest-the-dispute-by-dispute-id`, `simulate-payment`, `fetch-simulation`
- Payouts: `standard-transfer-v2`, `get-transfer-status-v2`, `batch-transfer-v2`, `get-batch-transfer-status-v2`, `authorize`, `create-cashgram`, `deactivate-cashgram`, `get-cashgram-status`
- SecureID: `verify-name-match`, `generate-kyc-link`, `get-kyc-link-status`, `generate-static-kyc-link`, `deactivate-static-kyc-link`

No subscription tools listed.

## Implementation notes for Nirantar

1. Pin `x-api-version: 2026-01-01` explicitly in the adapter; pin the webhook version in the dashboard to match the parser.
2. Send `x-idempotency-key` = Nirantar command ID on every create/charge call; map HTTP 422 to "idempotency conflict — fetch existing".
3. Use `ON_DEMAND` plans for EMI/variable billing, `PERIODIC` for fixed subscriptions; use controlled-notifications/executions when Nirantar owns pre-debit timing.
4. Reconfigure webhook retry policy to Exponential/Custom with max retries, and run a poller on `GET /subscriptions/{id}/payments` because subscription webhooks cannot be replayed.
5. Dedupe on `x-idempotency-header` (webhook) + (subscription_id, payment_id, event type).
6. Keep AI MCP tools read-only in production; `create-refund`, `accept-dispute...`, payouts tools require human approval.
