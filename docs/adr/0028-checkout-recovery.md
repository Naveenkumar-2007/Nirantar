# ADR-0028: Checkout drop-off recovery

Status: accepted (2026-10-08)

## Context

Indian D2C businesses lose a large share of carts at the payment step: UPI apps time out, banks have bad minutes,
customers get distracted. Most checkouts that drop off are never followed up; the rest are hit with generic
"you forgot something!" blasts and coupons. The brief asks for checkout drop-off recovery with the same bar as the
rest of Nirantar: bounded actions, stopping rules, an audit trail, and money that is measured, not claimed.

## Decision

1. **Two event sources, one record.**
   - The business's server reports `initiated`, `payment_page`, `payment_failed`, `paid` and `expired` to
     `POST /v1/checkout/events`.
   - It uses a store key with the `checkout_ingest` role. That key can do nothing else: no reads, no money, no
     customers.
   - Events are idempotent on `event_id`. Future-dated events are refused.
   - Provider webhooks on the checkout's own order count too: a failed attempt becomes a `payment_failed` event,
     and a capture closes the checkout as `paid`/`original`.
2. **One workflow per checkout** (`CheckoutRecoveryWorkflow`):
   - **Quiet period:** waits until the checkout has been quiet for 30 minutes. Any new event restarts the wait.
   - **Assessment, once per checkout:**
     - The cause is diagnosed from the decline reason: bank issue, insufficient funds, card problem,
       cancelled, repeated failures, or abandoned at cart or at payment.
     - Eligibility: there must be a customer to contact and the cart must be at least ₹100.
     - Each checkout is assigned to treatment or holdout, per customer, using the tenant's holdout share.
   - **Contact:**
     - One reminder worded for the cause, with a secure link for **exactly** the checkout's amount. Then at most
       **one** follow-up 20 hours later.
     - A step refused for the contact window is deferred to `retry_after`, at most twice.
     - Consent, opt-out or fatigue refusals end contact for that checkout.
   - **High value:** a checkout of ₹10,000 or more that is still open after the follow-up goes to a person
     (a `checkout` case), never to more messages.
   - **Expiry:** an open checkout expires after 72 hours.
3. **Cart reminders are marketing.**
   - Under WhatsApp's rules a cart reminder is Marketing, not Utility. Templates are registered as MARKETING, and
     the gateway requires WhatsApp **and** promotional consent.
   - Messages never contain offers or discounts, so no agent can invent an amount.
4. **Money is provider truth.**
   - A payment through a recovery link or pay page is verified with the provider (`verify_capture` for the
     checkout's amount).
   - It is booked once (DR clearing / CR income), and the checkout is marked `paid`/`recovery_link`.
   - A webhook replay is a no-op: the payment row is unique per provider payment id, and the ledger idempotency
     key is `settle:<provider>:<payment>`.
   - A "paid" reported by the business closes the checkout but is never booked, because it is the business's
     own sale.
5. **Measurement.** `GET /v1/checkout-recovery` returns:
   - the funnel: started → reached payment → dropped off → reminded → recovered;
   - the diagnosed causes;
   - verified recovered value;
   - the recovery rate of treatment against holdout, with a 95% CI, using `experiments.incremental` — the same
     statistics as every other proof in the product.

## Consequences

- The business integrates once: a few server-side calls from its checkout.
- Voice is not used for carts. A call about an abandoned cart is intrusive, and the TRAI rules for promotional voice
  differ. High-value carts go to a person instead.
- Migration `0032` adds `billing.checkout_sessions`, `ops.checkout_events`, `ops.checkout_chases` and
  `payment_requests.checkout_session_id`, all with RLS.
