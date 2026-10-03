# ADR-0007: Champion decisions for M4 (bank health) and M5 (uplift) on RecurSim

- Status: Accepted · Date: 2026-09-28 · Evidence: evals/results/ml_latest.json (source=recursim)

## M4 bank health

| Detector | Incident recall | False alarms / bank-week | Median delay |
|---|---|---|---|
| EWMA + binomial z (baseline) | 1.00 | 0.28 | 0h |
| BOCPD Beta-Binomial (advanced) | 0.85 | 0.00 | 0h |

At ~400 attempts/bank/hour a 2% degradation is statistically obvious, so the baseline wins on recall well within the false-alarm budget. **Champion: EWMA.** BOCPD is retained for low-volume banks/tenants where binomial z-scores are noisy (re-evaluated per tenant).

## M5 uplift

Unconstrained: with assumed costs (₹1 WhatsApp, ₹8 voice) "always voice" is within ~1% of the oracle, so targeting has little headroom and both learners lose to it.

Capacity-constrained (voice for 20% of failures — the realistic case: telephony concurrency, staff, fatigue):

| Scoring | Δ net ₹/failure vs random (6k / 20k customers) |
|---|---|
| Largest amount first (heuristic) | +158 / +156 |
| T-learner | +115 / +129 |
| DR-learner | +104 / +143 |
| Oracle | +231 / +209 |

Learned uplift improves with data but **does not beat the amount heuristic yet**.

## Decision

1. M5 is **not promoted**. It runs in **shadow** (scores logged, not acted on).
2. The Contact Arbiter's value input is `amount × prior_effect(arm, failure_code)` (the heuristic, with the code-level prior "no contact for BANK_TECHNICAL", which the oracle confirms).
3. The M5 gate is tightened: it must beat the amount heuristic under the tenant's actual capacity, on the tenant's own randomized holdout data, before promotion.
4. These are simulator findings; they justify the *mechanism*, not any claim about real revenue.
