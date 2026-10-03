# WhatsApp Business Platform — Cloud API — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Meta page during this session.

## Source URLs (retrieved 2026-09-28)

- Webhooks getting started (verification, signature, retries): https://developers.facebook.com/docs/graph-api/webhooks/getting-started
- Service messages / send API: https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages
- Template fundamentals: https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
- Pricing: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- Error codes: https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes
- Throughput (via official search snippet): https://developers.facebook.com/documentation/business-messaging/whatsapp/throughput
- Messaging limits: https://developers.facebook.com/docs/whatsapp/api/rate-limits/ (found in search, body not fetched)
- Opt-in / 24h window (via official search snippets): https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages, https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
- Per-user marketing limits: https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/marketing-templates/per-user-limits/ (found in search)

## API version

- Graph API path-versioned; current docs examples use **`v25.0`** (e.g., `POST /v25.0/{PHONE_NUMBER_ID}/messages`). Version deprecation schedule — **UNVERIFIED**.

## Authentication

- `Authorization: Bearer <access token>`; system user access token recommended for server use. Error 190 = token expired.
- Webhook: App Secret used for signature; separate Verify Token for the subscription handshake.

## Relevant endpoints (Nirantar scope)

- `POST https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}/messages` — send template or service (free-form) messages. Response = acceptance only; delivery via webhooks.
- `POST/GET /{WABA_ID}/message_templates` — create / list templates (automatic review → status `APPROVED`).
- Media upload, phone-number and WABA management endpoints — **UNVERIFIED** (not fetched).

## Webhook events

- Verification handshake: `GET` with `hub.mode=subscribe`, `hub.challenge`, `hub.verify_token`; respond with the `hub.challenge` value if the token matches.
- Signature: header **`X-Hub-Signature-256`** = `sha256=<hex>`; HMAC-SHA256 of the **raw payload** keyed with the **App Secret** (the `sha256=` prefix and hex encoding are standard Meta behaviour; exact prefix wording **UNVERIFIED** on the retrieved page).
- Payloads batch up to 1000 updates. The `messages` field carries inbound messages and message `statuses` (`sent`, `delivered`, `read`, `failed`). Template status/quality update fields (e.g., `message_template_status_update`) — **UNVERIFIED** names.

## Rate limits

- Throughput: **80 messages/second per business phone number by default** (upgradable) — per official search snippet.
- Error 130429 = throughput reached; 131056 = pair rate limit (too many messages to the same recipient in a short time); 4 = app API call rate limit; 80007 = WABA rate limit.
- Business-initiated messaging limits (tiers of unique recipients per 24 h) — exact tiers **UNVERIFIED**.
- Per-user marketing template limits exist (error 131049).
- Template count: 250 (unverified business) up to 6,000 (verified with approved display name).

## Idempotency behavior

- No idempotency key on send — **UNVERIFIED** that one exists. Retrying a send can produce duplicate messages; Nirantar must dedupe on its side (store `wamid` returned per logical notification before retrying).
- Webhook dedupe: use message id (`wamid`) + status.

## Retry behavior (provider-side webhook retries)

- Failed deliveries: retried immediately, then a few more times with decreasing frequency **over 36 hours**; dropped after 36 h. Respond `200 OK` (HTTPS).
- Undelivered outbound messages: default TTL **30 days**; authentication templates 10 min.

## 24-hour customer service window

- Opens when the user messages or calls the business; resets on each user message/call; lasts 24 h.
- Inside window: any service (non-template) message types; outside: **only approved templates**. Violation → error **131047** ("More than 24 hours have passed since the recipient last replied").
- 72-hour Free Entry Point window for Click-to-WhatsApp ads / Page CTA entries.

## Opt-in requirements

- Must obtain user opt-in **before** sending templates; opt-in must clearly state the business name and intent; only message users who opted in (per official search snippets from Meta docs). Detailed opt-in policy page — **UNVERIFIED** (not fetched).

## Pricing (relevant)

- Per-message pricing since **1 July 2025**; charged on template delivery by category (marketing / utility / authentication) and recipient country.
- Free: non-template messages within the service window; **utility templates within an open service window**; everything within a 72 h FEP window.
- India: INR billing available from 1 Jan 2026; eligible businesses must migrate to INR by **31 Dec 2026**. INR rate card values — **UNVERIFIED**.

## Sandbox / test-mode behavior

- Meta provides a test phone number/WABA for development (from Get Started guide in search results) — details (recipient allow-list size, etc.) **UNVERIFIED**.

## Error model

- Error object: `code` (use for logic), `message`, `error_data.details`, `fbtrace_id` (quote to support). Key codes: 131047 (outside window), 131049 (ecosystem/marketing limit — wait 24 h), 131056 (pair rate), 130429 (throughput), 131026 (undeliverable), 132000 (template param count mismatch), 132001 (template missing/unapproved in language), 131048 (quality-based restriction), 368 (WABA restricted), 190 (token expired), 4, 80007.

## Limitations

- Payment reminders must be approved **utility** templates; Meta may re-categorise to marketing (pricing/limits impact) — re-categorisation rules **UNVERIFIED** in this session.
- Templates can be paused/disabled on low quality; inactive templates archived after 12 months.
- Marketing templates are subject to per-user limits and may silently not deliver (131049).

## Security considerations

- Verify `X-Hub-Signature-256` with constant-time compare on raw bytes; reject if missing.
- Use a long random `hub.verify_token`; rotate system-user tokens; store in secrets manager.
- WhatsApp messages about dues are personal financial data — minimise content (no full account numbers), comply with DPDP consent records.

## Implementation notes for Nirantar

1. Record explicit WhatsApp opt-in (timestamp, source, wording) per customer; block template sends without it.
2. Model the service window per (business number, customer) from inbound webhooks; choose free-form vs template automatically; handle 131047 by falling back to a template.
3. Pre-register utility templates for: pre-debit notice, payment failed, mandate registration link, payment link, receipt. Keep them transactional to stay in utility category.
4. Webhook inbox with dedupe on `wamid`+status; tolerate 36 h replays and out-of-order statuses.
5. Outbound queue throttled below 80 mps/number and per-recipient pacing to avoid 131056.
