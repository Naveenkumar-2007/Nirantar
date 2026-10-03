# Model card — M1 Debit Failure Predictor

- **Purpose:** probability that a scheduled debit fails, predicted 3 days before execution, so the
  Pre-debit Guardian can intervene. Never used alone to take a money action.
- **Feature version:** `m1-features-v1` (point-in-time; leakage test in tests/ml)
- **Data provenance:** {'source': 'recursim', 'seed': 7, 'customers': 4000, 'months': 12}
- **IMPORTANT:** trained and evaluated on **synthetic RecurSim data**. These numbers show the pipeline
  works; they are **not** production performance.
- **Temporal split (months):** {'train': [1, 2, 3, 4, 5, 6, 7], 'valid': [8, 9], 'test': [10, 11]} · rows {'train': 21638, 'valid': 5689, 'test': 5276}
- **Test base rate:** 0.131

| metric (test) | baseline LR | LightGBM + isotonic |
|---|---|---|
| auc | 0.7397 | 0.7524 |
| pr_auc | 0.3563 | 0.3694 |
| brier | 0.1004 | 0.0975 |
| ece | 0.0125 | 0.0114 |

**Gate:** PASSED 

**Top features (gain share):**
- `log_amount`: 0.163
- `fail_rate_last3`: 0.135
- `fail_rate_all`: 0.127
- `bank_td_rate_24h`: 0.123
- `bank_td_rate_7d`: 0.099
- `days_since_last_failure`: 0.051
- `cash_confidence`: 0.045
- `cash_day_distance`: 0.044

**Known limitations:** no real customer data yet; advanced sequence model (TFT) deferred (ADR-0006);
per-tenant retraining required before any tenant use (ADR-0004 f).
**Monitoring:** PSI drift on inputs, calibration (ECE) on weekly labelled outcomes; retrain on drift or ECE > gate.
**Rollback:** re-point the `champion` alias to the previous registered version.
