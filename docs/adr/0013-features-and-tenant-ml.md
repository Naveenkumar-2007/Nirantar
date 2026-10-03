# ADR-0013: Feature store, per-tenant training, rollout and monitoring (Phase 2 · P3)

- Status: Accepted · Date: 2026-09-29
- Evidence: tests/ml/test_tenant_ml_lifecycle.py (planted-signal lifecycle + online/offline parity, real
  Postgres/Iceberg/Redis/MLflow), tests/integration/test_api.py, runs on RecurSim-backed merchants (below).

## Decisions

1. **Feature store without Feast (for now).** Every Feast release ≥ 0.50 requires `pandas<3` (resolver output,
   2026-09-29); the codebase is on pandas 3.0.6. We built the parts Feast would give us, narrowly:
   - versioned feature definitions (`features/definitions.py`, `subscription_payment:v1`, 27 features);
   - ONE transformation (`features/compute.py`) used offline and online; leakage is prevented *inside* it
     (first-attempt outcome only if before `as_of`; recovery only if paid before `as_of` or its 30-day horizon
     closed) — callers cannot leak by accident;
   - offline store = point-in-time training sets in Iceberg (`gold.training_m1`, snapshot id in the model record);
   - online store = Redis holding each entity's recent HISTORY + tenant recent events, not precomputed values;
     features are computed at request time by the same function → no training/serving skew by construction
     (`test_online_features_equal_offline_training_features`).
   Revisit Feast when it supports pandas 3.
2. **Real tenant-level signals replace v1 approximations**: bank/tenant technical-failure rates come from the
   tenant's own attempts (previously hard-coded 0); `live_features.py` is no longer in the decision path.
3. **Cold start = a transparent prior**: the subscription's failure rate shrunk towards the tenant's 30-day rate
   (k = 5); with no tenant data, `ml.cold_start_failure_rate` (0.15, an explicitly labelled assumption in
   platform.yaml). The synthetic global model is research only (Research benchmarks page).
4. **Per-tenant training** only when data-health readiness passes and there is a reason (first model, monitoring
   trigger, or ≥ 25% more labels after a rejection). Temporal split 60/10/10/20 (train / calibration / selection /
   test); candidates logistic and LightGBM, each isotonic-calibrated on its own window; the better on selection
   Brier must beat the prior on the untouched test window (gates `m1_debit_failure_tenant`, plus a paired
   bootstrap CI on Brier). MLflow name `m1_debit_failure.<tenant>`, skops with a reviewed allow-list.
5. **Rollout on live verified outcomes**: the router scores every live version on every request (decision /
   shadow / reference roles, feature values logged), so all comparisons are paired on the same debits.
   shadow → canary (20% by stable hash) → champion when the 95% upper bound of ΔBrier ≤ 0.002; automatic rollback
   to the prior on live ECE > 0.08 or significant loss to the prior; manual retire with a reason. Every stage
   change → `ai.model_events` + audit chain.
6. **Monitoring**: Evidently DataDriftPreset (served features vs the model's training window), live metrics per
   version, retrain triggers (drift ≥ 30% of features, +25% labels, rollback) → `ai.model_monitoring`.
7. **Orchestration**: Dagster assets `feature_store → m1_training → m1_rollout → m1_monitoring` after the data
   assets, same tenant partitions and run records.

## Results so far (honest)

| Tenant (all synthetic) | Cycles | Learned test AUC / Brier | Prior AUC / Brier | Outcome |
|---|---|---|---|---|
| RecurSim merchant, 600 customers | 5,338 | 0.727 / 0.1029 | 0.716 / 0.1025 | rejected — does not beat the prior |
| RecurSim merchant, 3,000 customers | 26,575 | 0.714 / 0.0967 | 0.698 / 0.0967 | rejected — ties the prior |
| Planted-signal merchant (test) | 9,600 | beats prior by > 0.05 AUC | — | shadow → canary → champion → rollback |

RecurSim's failures are mostly random cash shocks: even the old global model with the simulator's internal
variables reached only AUC 0.752 vs 0.740 for logistic. So on simulator merchants the prior is the right model
and the system correctly keeps it. A first version of the selection step calibrated LightGBM on the same rows it
was selected on (validation ECE exactly 0.0); fixed by the 4-way split before any result was accepted.

## Known limits

- One model (M1) on the new lifecycle; M3/M6/M13–M17 follow on the same framework (P4 adds churn).
- Online events are capped at 31 days per tenant in one Redis value; very large tenants need per-day buckets.
- Serving runs in-process (worker/API) via MLflow; a separate model server (BentoML) is deferred until latency or
  isolation requires it.
- Nirantar-scheduled debits have no method/bank at T-3 → `unknown` (consistent in training and serving).
