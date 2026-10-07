# ADR-0023: Pilot readiness — deploy kit, CSV import, receipts, promise-to-pay (P8.4 kit, P8.7, P10 first front)

- Status: Accepted · Date: 2026-10-07
- Evidence:
  - **Deploy kit:**
    - `docker compose -f deploy/compose.prod.yml config` is valid (12 services).
    - Both production images build. The Python image imports the API, services, workflows and alembic. The web
      container serves with the security headers.
  - `tests/unit/test_ratelimit.py`.
  - `tests/e2e/test_business_loop.py`:
    - CSV import: dry run, per-row problems, real import with enrollment, duplicate refusal.
    - A pay-page payment settles from the Razorpay webhook alone (order id, no notes).
    - Receipts: one per verified payment, in the customer's language, carrying the provider payment reference.
    - Promise-to-pay: "parso pay karunga" is recorded with its date; chasing pauses; a reminder goes out on the
      day; kept when paid; broken when not; recovery resumes within the fatigue budget.
  - `tests/unit/test_promise_dates.py`: 34 phrasings in English, Hinglish, romanised Telugu, Devanagari and Telugu
    script; amounts are never read as dates.
  - `tests/unit/test_policy_engine.py`: customer-requested contact skips only the fatigue budget.

## Decisions

1. **Deploy kit (no domain needed until go-live).**
   - One server, `deploy/compose.prod.yml`. Caddy is the only listener, with automatic HTTPS.
   - Public paths are `/pay/*` (web) and `/webhooks/*` (API). The API's `/v1` is not routed from the internet.
   - The Keycloak admin console is not exposed.
   - Non-root images. Migrations run as a one-shot service before the API and services start.
   - Nightly `pg_dump` of every database, kept 14 days, with an instruction to copy it off the server.
   - All secrets come from `deploy/.env`, which is git-ignored. Production Postgres roles get their passwords from
     it, and the S3 config is generated at start.
2. **Rate limits** on the unauthenticated surfaces: a 1-minute sliding window per client IP.
   - Confirmations: 10/min. Pay-page views: 60/min. Webhooks: 1,200/min.
   - Signed-in traffic is bounded by identity instead.
   - It is in-process, which is right for one API instance. Replicas need Redis behind the same rules.
3. **CSV import.**
   - Always a dry run first. Every check runs on every row, so one upload shows the founder every problem.
   - Valid rows import in one transaction.
   - Duplicates are found by the keyed phone hash: within the file and against existing customers.
   - Consent is never assumed. `yes` needs `consent_source`, and the import records `via: csv_import`.
   - Enrolling needs `subscriptions:write`.
4. **Webhook attribution by order id.** Razorpay does not reliably copy order notes onto the payment, so
   `find_debit` falls back to the order Nirantar recorded against the debit.
5. **Receipts = RBI post-debit notification.**
   - After a verified pay-by-link payment, `comms.send_receipt` runs as the billing agent with
     `mandatory_kind=postdebit_notice`, once per debit (idempotency key).
   - Provider-run subscriptions get the provider's own receipts.
6. **Promise-to-pay.**
   - Intent: the reply classifier understands Hinglish and Telugu promise verbs.
   - Date: a deterministic extractor (kal, parso, weekdays, "15 tarikh", "salary", "in N days", in several
     scripts). It returns None rather than guess, and ignores promises more than 30 days out.
   - `ops.promises` records the promise in the customer's words. A newer promise supersedes an older one.
   - DebitCycleWorkflow, behind `workflow.patched("promise-to-pay-v1")`:
     - a new promise interrupts any wait and takes over;
     - no chasing until the promised day;
     - a `whatsapp.promise_reminder` that morning, with a link;
     - polling until the end of the day;
     - kept (provider-verified) or broken;
     - a broken promise resumes recovery.
   - A promise may extend the recovery window, but not past 30 days.
7. **"Customer-requested" contact** is a new, explicit policy flag. The reminder on the day the customer named is
   exempt from the fatigue budget only. Consent, opt-out, contact window and conduct rules still apply. A broken
   promise does not unlock extra contact: the next recovery round is again subject to the budget, as the e2e test
   proves.

## Consequences

- The new WhatsApp templates (`payment_due`, `payment_receipt`, `promise_reminder`) must be submitted to Meta
  before they can reach customers outside the 24-hour window.
- The Python image is 2.9 GB because it carries the ML and data stack. A slim API-only image is a later
  optimisation.
- Owner daily summary, bank-degradation holds, B2B receivables and checkout recovery remain on the plan (P10 and
  later).
