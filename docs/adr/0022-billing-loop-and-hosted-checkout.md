# ADR-0022: Nirantar runs the billing loop — plans, enrollment, pay-by-link, hosted checkout (P8.6)

- Status: Accepted · Date: 2026-10-07
- Why: before this phase, nothing in production created debits; only the demo seed did. A real business could
  not go from "my customers" to "money collected and verified" without a provider subscription product. Razorpay
  Subscriptions is not enabled on the test account, and the account then hit Razorpay's 30-payment-link test limit.
- Evidence:
  - `tests/e2e/test_business_loop.py`:
    - plan → customers (consent needs its evidence) → enrollment creates the first debit → the event bridge starts
      DebitCycleWorkflow → the due-date message carries the link in the customer's language;
    - one customer pays with no webhook: the 30-minute poll finds it, it is verified, and the debit closes as
      paid_on_time;
    - the other does not pay, gets NOT_PAID triage and a recovery reminder with a fresh link, pays it, the poll
      finds it, and the debit closes as recovered;
    - the billing clock creates the next cycle once; cancellation stops future charges;
    - the manual "send link now" reuses an open link, and asking for money already paid is refused;
    - the hosted pay page shows only first name, plan, amount and due date; a tampered token is a 404, a forged
      checkout signature is refused, and a valid one settles the debit.
  - Replay: the old DebitCycleWorkflow history still replays (the new path sits behind `workflow.patched`), and
    the new pay-by-link and BillingSweepWorkflow histories are recorded.
  - Live, against Razorpay TEST, with messaging mocked:
    - plan, customer and enrollment on the E2E Chai Club business;
    - a real Razorpay order was created and the pay page rendered on mobile;
    - Razorpay Checkout opened for the order (₹1).
    - The final click inside Razorpay's iframe has to be done by a person: the automation browser cannot click
      into Razorpay's CSS-scaled frame.

## Decisions

1. **Nirantar owns the schedule.**
   - `billing.plans` holds the product. A subscription records its plan and a `collection_method`.
   - `ensure_debits` creates every missing debit due within 3 days, advances `next_charge_on` by the interval
     (month-end aware), and never charges missed past dates retroactively.
   - It runs at enrollment and in BillingSweepWorkflow, a Temporal schedule every 6 hours.
   - Each debit emits `subscription.debit_scheduled`, so prediction, notice, collection, verification and recovery
     reuse the existing DebitCycleWorkflow.
2. **Collection methods.**
   - `payment_link` works on every account.
   - `provider_subscription` means the provider charges.
   - `mandate` is modelled but **refused at plan creation** until Nirantar charges mandate tokens. We never offer
     a method that cannot collect.
3. **Collection inside DebitCycleWorkflow** (behind `workflow.patched("pay-by-link-collection-v1")`, so it is
   replay-safe):
   - Due date: `billing.send_payment_request` runs through the gateway (consent, contact window and fatigue
     apply). If the window is closed, it waits for it and retries.
   - Then it polls every 30 minutes until paid. A webhook, when configured, ends the wait at once.
   - Not paid by the deadline becomes `NOT_PAID` / `not_paid_by_due_date`: nothing was declined, so there is no
     retry recommendation, only contact.
   - Recovery waits keep polling for these debits.
4. **Every link Nirantar creates for a debit is tracked** in `billing.payment_requests`, including the recovery
   agent's own links. Reconciliation maps a provider payment to the debit through this record, not through notes
   the provider may not copy.
5. **Hosted pay page.**
   - Used when the provider declares `CHECKOUT_ORDERS` (Razorpay Orders + Standard Checkout) and
     `NIRANTAR_PUBLIC_APP_URL` is set. Otherwise the provider payment link is used.
   - The URL token is `tenant.request.keyed-hash`, with the hash keyed by the business's own key.
   - The public endpoints return only what the customer needs.
   - Confirmation: the Razorpay signature must verify with the key secret. The payment is then fetched from
     Razorpay and applied through the idempotent path. The browser's word is never trusted.
6. **`NOT_PAID` joins the failure categories.** Effect priors saved before this phase inherit the business's own
   `UNKNOWN` prior for it (the most cautious), so existing settings keep validating.
7. **Consent evidence.** "WhatsApp consent: yes" must say how the customer agreed. The evidence (source, who
   recorded it, when) is stored with the customer, and policy reads only true/false flags, so metadata can never
   count as consent.
8. **UI.**
   - Plans page.
   - "Add customer" with consent capture.
   - Customer 360: put on a plan, payment links with status and copy, send link now, check payments, cancel.
   - Public `/pay/[token]` in English, Hindi and Telugu.

## Consequences

- Real customers need the pay page on a public URL (P8.4). Locally it is `http://localhost:3010`.
- The `whatsapp.payment_due` Meta template must be submitted and approved before due-date messages can reach
  customers outside the 24-hour window. Until then the link is created and shown in the dashboard to share.
- Mandate charging (UPI AutoPay / e-mandate tokens) is a later phase. It needs Razorpay Recurring Payments
  enabled on the account.
