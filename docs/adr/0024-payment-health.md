# ADR-0024: Payment health — issuer outages detected, customers not blamed, money recovered (P10)

- Status: Accepted · Date: 2026-10-07
- Brief direction: "Payment degradation → root cause → recovery action".
- Evidence:
  - `tests/unit/test_payment_health.py`:
    - an outage is detected with its start hour; steady noise is not an incident; a recovered issuer reads healthy;
    - one unlucky customer is not an outage (volume gate);
    - issuer mapping covers UPI handles (PSP bank), net banking, e-mandate, wallet and card;
    - only technical declines count.
  - `tests/e2e/test_payment_health.py`:
    - setup: two businesses share an issuer over about 70 hours of traffic, then a 4-hour 40% technical-failure
      outage. Three real debits are declined through signed webhooks;
    - the scan opens one incident with the right start; the failure is triaged BANK_TECHNICAL and the customer is
      not contacted;
    - the issuer recovers; PaymentHealthWorkflow closes the incident and sends each of the three affected
      customers one honest message with a fresh link, and nobody at the other business;
    - one pays; `/v1/payment-health` reports affected 3, contacted 3, recovered 1, ₹499.

## Decisions

1. **Unit of health = (rail, issuer).**
   - The issuer comes from provider fields only: net banking or e-mandate bank code, the UPI handle's PSP bank,
     the wallet, the card issuer. It is stored on `billing.payments.issuer`.
2. **Platform-wide, aggregates only.** Every business's payments are read inside its own RLS scope; only hourly
   counts leave it.
   - `core.payment_incidents` holds issuer, rail, rates and times, with no business or customer data.
   - A small business benefits from everyone's traffic.
3. **Detector.**
   - Onset: M4's Bayesian online change-point detector (Beta-Binomial, Adams & MacKay).
   - State: the trailing run of hours failing at ≥ 4× the issuer's robust baseline (the median hour), and at least
     3 points above it, with ≥ 8 attempts. A change-point model stops alarming once the bad rate is the new normal,
     so it decides only that an incident began. The elevated recent rate decides that it is still going on.
   - The incident closes after 2 healthy hours.
4. **Explain.** `handle_failure` looks up the incident covering the failed payment's issuer and time. Triage then
   says BANK_TECHNICAL, with no customer contact. The bank's fault is not chased as the customer's.
5. **Recover.**
   - PaymentHealthWorkflow runs every 15 minutes (a Temporal schedule).
   - For each closed incident not yet recovered, each business's affected debits get `billing.send_payment_request`
     with occasion "incident" and the `whatsapp.bank_issue_retry` wording: "a temporary problem at the bank… it's
     working again".
   - Everything goes through the gateway (consent, window, fatigue, audit), idempotent per incident and debit, and
     recorded in `ops.incident_recoveries`.
6. **UI.** The Payment health page shows live incidents, failure rate against normal, duration, and per incident:
   your customers hit, sent a retry, and paid (verified ₹).

## Consequences

- Detection quality depends on volume. A pilot with few payments per issuer per hour will rarely cross the volume
  gate; that is deliberate, because false outages would stop legitimate recovery.
- Retrying mandate debits automatically after an outage needs mandate charging (later). Today the recovery action
  is the honest message with a link.
