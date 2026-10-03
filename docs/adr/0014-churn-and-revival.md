# ADR-0014: Churn lifecycle, churn models, lifetime value and the win-back loop (Phase 2 · P4)

- Status: Accepted · Date: 2026-10-01
- Evidence: tests/unit/test_lifecycle_and_offers.py, tests/ml/test_clv_sbg.py (parameter recovery + Monte-Carlo check
  of the residual-value sum), tests/integration/test_churn_scoring.py, tests/e2e/test_revival_loop.py (full loop on
  Postgres + Iceberg + Temporal), runs on the 3,000-customer RecurSim merchant (below).

## Decisions

1. **Lifecycle labels (`gold.subscription_lifecycle`)**, in order of trust: provider end states (`cancelled`,
   `halted`, `expired` → churn; `completed` → natural end, NOT churn; `ended_at` per Razorpay's subscription
   entity), Nirantar subscription status, then inferred lapse (no attempt for 1.5 × the entity's billing period +
   7 days; churn_at = the missed next due). Type: *involuntary* if the last cycle failed and was never paid (or
   provider `halted`), *voluntary* otherwise. Active subscribers are right-censored, never "didn't churn".
2. **M15 lifetime value = shifted-beta-geometric (Fader & Hardie 2007)** fitted per tenant by maximum likelihood
   with right-censoring; forward CLV = Σ P(still paying at period n+j | paid n)·(1+d)^−j × amount, written as an
   explicit sum and verified against a Monte-Carlo simulation of the model. Goodness of fit = fitted vs
   Kaplan–Meier retention (a first KM version wrongly kept censored subscribers in the risk set; caught by the
   parameter-recovery test and fixed).
3. **M6 churn-in-60-days and M13 churn-reason** run on the P3 framework, now generic (`ml/specs.py`: training set,
   transparent baseline, gates, readiness, live label per model). Baselines: M6 = the sBG tenure model **fitted only
   on data known at the training cutoff** (so it cannot see the test period); M13 = a calibrated
   "recent payment trouble" rule. Same shadow → canary → champion rollout; live labels are written later from the
   lifecycle (`churned_60d`, `churn_involuntary`, source `lifecycle`).
4. **At-risk = relative**: probability ≥ max(floor, lift × the tenant's average) (defaults 5% and 2×). An absolute
   threshold (first version: 30%) flags nobody at a typical 5%/month churn.
5. **Win-back loop** (RevivalWorkflow, Temporal): candidates are filtered on pre-treatment facts only (churned in
   window, contactable Nirantar customer, WhatsApp + **promotional** consent, 90-day cooldown, offer limit, capacity
   by sBG value) BEFORE stratified randomisation — involuntary churners get only the reminder arm (a discount does
   not fix a failed mandate), voluntary churners every offer arm; each stratum has its own holdout (≥ 5%).
   Offers are money: the discounted amount is computed by code from the subscription amount (whole rupees), never
   by an LLM or the caller; discounts above `policy.discount_approval_above_minor` need a second person
   (NIR-GOV-MAKER-CHECKER-001). Messages are promotional (IN-TRAI-TCCCPR-DLT-001): consent, contact windows (the
   workflow waits for the window), fatigue, template registry, opt-out line, no ₹ figures in text.
   Reactivation is measured identically in every arm (any captured payment by the customer after the case
   opened), so the holdout comparison is intention-to-treat. Reactivation payments are verified against the
   OFFERED amount and posted clearing → income (nothing was receivable after churn).
6. Found by the loop test and fixed: payments outside a debit/offer carried no customer, which would have made
   spontaneous returns invisible and flattered the offers against the holdout.
7. Local reliability: all compose services now `restart: unless-stopped` (Docker Desktop restarts took them down).

## Results on the RecurSim merchant (3,000 customers, synthetic)

| | Result |
|---|---|
| Lifecycle | 2,975 subscriptions · 1,001 churned (782 payment-driven, 219 by choice) |
| sBG | α 0.47, β 8.99 (≈5% churn/period, strong heterogeneity); max gap to Kaplan–Meier 1.25 pts → fit ok |
| M6 learned vs sBG | AUC 0.619 vs 0.510 but Brier 0.0428 vs 0.0386 → **rejected** (ranks better, probabilities worse) |
| M13 learned vs rule | AUC 0.658 vs 0.619, Brier better, ECE 0.090 > 0.08 → **rejected** |
| At-risk list | none: the tenure-only baseline cannot single out individuals (max ≈ average); needs a promoted M6 |

## Known limits

- Retention outreach to ACTIVE at-risk subscribers is listed with a route (fix payment vs retention offer) but not
  automated; only churned subscribers enter the win-back workflow.
- Win-back workflows need a running Temporal worker with RevivalActivities (packaged as a service in P5); the
  daily Dagster asset creates cases and starts workflows idempotently.
- Offer arms are edited through the settings API (`retention` namespace); the Automations page has no offer-grid
  form yet. M14 (per-customer uplift targeting) waits for enough randomised win-back outcomes; until then the arm
  comparison is the experiment table.
