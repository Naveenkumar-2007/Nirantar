# ADR-0003: Payment provider strategy

- Status: Accepted · Date: 2026-09-28 · Traces: BB-§10, BB-§11 · Evidence: docs/integrations/*.md

## Context (verified 2026-09-28)

- Stripe: new Indian accounts invite-only; no automatic retries for India-issued cards; e-mandate card charges have a fixed 26h pending window; UPI AutoPay capped at ₹15,000.
- Razorpay: webhook signature HMAC-SHA256 over raw body (`X-Razorpay-Signature`), dedupe header `x-razorpay-event-id`, 5s response timeout, retries for 24h then **auto-disables** the webhook. No general idempotency header (refund `receipt` only). UPI AutoPay registration requires Intent flow from 28 Feb 2026.
- Cashfree: `x-api-version` 2026-01-01, `x-idempotency-key` supported, webhook signature over timestamp + raw body, short retry schedule, no replay for subscription webhooks.

## Decision

1. **Razorpay is the primary rail, Cashfree secondary, Stripe optional** (only for merchants that already have an account).
2. Webhooks go to a single inbox: verify → persist raw → 2xx within ~1s → process asynchronously.
3. **Every provider gets a reconciliation poller.** Webhooks are a latency optimisation, not the source of truth. Missed or disabled webhooks are healed by polling provider state.
4. Nirantar enforces idempotency itself (`core.idempotency_keys` + ledger keys) and additionally sends provider idempotency keys where supported.
5. Capabilities are declared per adapter; workflows check capabilities instead of assuming parity.
6. Agents only get read and "prepare" access to money-moving tools by default; execution needs policy ALLOW and, above thresholds, a human approval token.
