# ADR-0006: ML platform for milestone M-C

- Status: Accepted · Date: 2026-09-28 · Traces: BB-§17–21

## Decisions

1. **RecurSim is the first training/evaluation source**, and it exposes true potential outcomes for every intervention arm. Results on it are labelled `source=recursim` everywhere and are never reported as production performance (BB-§18, §49).
2. **Temporal splits only** (train on earlier months, validate/test on later months). Random splits leak future behaviour in recurring-payment data.
3. **Point-in-time features**: every feature for a debit is computed from rows strictly before its prediction cutoff (T-3 days). A leakage test mutates future rows and asserts features don't change.
4. **Model choices now** (advanced models come when data volume/variety justifies them):

| Model | Baseline | Advanced (now) | Deferred |
|---|---|---|---|
| M1 debit failure | logistic regression | LightGBM + isotonic calibration | Temporal Fusion Transformer (needs long real sequences) |
| M2 cash window | modal success day | circular statistics with confidence | Bayesian periodicity |
| M4 bank health | EWMA z-score | Bayesian online changepoint detection (Beta-Bernoulli) | — |
| M5 uplift | T-learner (LightGBM) | DR-learner (doubly robust, LightGBM) | Causal forest / DragonNet |
| M10 cash forecast | Σ amount × P(success) | + split-conformal intervals | N-BEATS residual model |

   DR-learner is implemented directly rather than via EconML to avoid a heavy dependency; EconML remains an option (ADR update if adopted).
5. **MLflow** with a local SQLite backend (`ml/mlflow.db`) for tracking and the model registry. Promotion requires the offline gate in `ml/gates.yaml`.
6. **Per-tenant models by default** (ADR-0004 f). Global models train only on RecurSim + licensed public data.
