# ADR-0029: UPI AutoPay / e-mandate collection and the retry sequencer

Status: accepted (2026-10-08)

## Context

Until now, plans could only collect by payment link: mandate collection was switched off ("never offer what cannot
collect"). Recurring revenue in India is moving to UPI AutoPay and e-mandates. The common way to lose that revenue is
the blind retry: the same mandate is charged again a few hours later regardless of why it failed. That annoys banks,
customers and NPCI, and it rarely works.

## Decision

1. **Nirantar charges the mandate itself** (`collection_method = 'mandate'`).
   - Enrolment requires an **active**, provider-confirmed mandate whose known limit covers the plan amount and which
     is valid at the first charge. Otherwise enrolment is refused with the way forward: send a registration link,
     or collect by payment link.
   - On the due date the workflow calls `mandate.charge_debit` for attempt 1. On Razorpay this creates an order and
     then a recurring payment on the customer's token.
2. **Every charge is guarded** by the `mandate.charge_debit` tool, available only to the `retry_sequencer` agent.
   A charge is refused unless:
   - the debit is not already paid;
   - the mandate is active and unexpired, and its known limit covers the amount;
   - the attempt is within the cap of 3 (the first charge plus two retries);
   - a pre-debit notice reached the customer **at least 24 hours** before the charge (RBI e-mandate framework).

   The amount is always the debit's own; no agent can choose it. `(tenant, debit, attempt)` is unique, and the
   provider receipt is `<debit>.a<attempt>`, so an attempt can never be charged twice.
3. **The retry sequencer never retries blindly** (`mandates/retry.py`, a pure function):
   - **Bank technical failure:** retry the first morning after the notice lead, or one day later if a payment-health
     incident is still open for that bank.
   - **Insufficient funds:** retry three days later, or on the 1st of next month (salary credit) if that is within
     6 days.
   - **Anything else** (revoked or expired mandate, over the limit, card problems, unknown): **no retry**. Repeating
     cannot succeed; the existing recovery takes over (mandate repair, contact, payment link).
   - Retries happen only in the 06:00–09:00 IST window.
   - Each planned retry sends `mandate.notify_retry` (the mandatory pre-debit notice, with the amount and charge
     date) as soon as it is planned. If the notice cannot be sent, the attempt is cancelled and no charge happens.
4. **Outcomes are provider truth.**
   - Webhooks update the attempt (`charging` → `captured` / `failed`) and the debit through the usual verified path.
   - Without a webhook, reconciliation asks the provider for the attempt's order payments
     (`reconcile_mandate_attempts`).
   - A capture is verified and settled once.
5. **Replay-safe.** The retry loop runs before the contact rounds, behind `workflow.patched("mandate-retry-v1")`,
   with a recorded history (`DebitCycleWorkflow_mandate_retry`).

## Consequences

- Migration `0033` adds `billing.debit_attempts` (RLS). Every attempt is visible with its plan, reason, notice time,
  charge time and outcome: the audit trail for "why was the customer charged on that day".
- Mandate discovery now reuses an existing mandate record for a known token instead of colliding with it.
- Razorpay's recurring-payment API needs the provider customer id. The contact and email are sent when known.
  Live-mode charging requires the business's Razorpay recurring-payments feature to be enabled.
