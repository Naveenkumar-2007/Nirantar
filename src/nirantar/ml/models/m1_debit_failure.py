"""M1 Debit Failure Predictor (T-3 days).

Serving contract
  input : DataFrame with features.NUMERIC + features.CATEGORICAL (feature_version m1-features-v1)
  output: P(debit fails) in [0, 1], calibrated
Label   : failed (debit not captured at execution)
Split   : temporal — train < valid < test months
Baseline: logistic regression (one-hot + scaling)
Advanced: LightGBM + isotonic calibration fitted on the validation months
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.compose import ColumnTransformer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from nirantar.ml.features import CATEGORICAL, FEATURE_VERSION, NUMERIC
from nirantar.ml.metrics import classification_report

MODEL_NAME = "m1_debit_failure"
# Explicit allow-list for skops (safe deserialisation): only these types may be loaded from the registry.
TRUSTED_TYPES = [
    "collections.OrderedDict",
    "lightgbm.basic.Booster",
    "lightgbm.sklearn.LGBMClassifier",
    "nirantar.ml.models.m1_debit_failure.M1Model",
]


class M1Model(BaseEstimator, ClassifierMixin):
    """LightGBM with isotonic calibration. Picklable; logged with mlflow.sklearn."""

    def __init__(self, n_estimators: int = 400, learning_rate: float = 0.05, num_leaves: int = 31,
                 min_child_samples: int = 40, random_state: int = 7) -> None:
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.num_leaves = num_leaves
        self.min_child_samples = min_child_samples
        self.random_state = random_state

    def _frame(self, x: pd.DataFrame) -> pd.DataFrame:
        f = x[NUMERIC + CATEGORICAL].copy()
        for c in CATEGORICAL:
            f[c] = pd.Categorical(f[c].astype(str), categories=self.categories_[c])
        return f

    def fit(self, x: pd.DataFrame, y: np.ndarray, x_cal: pd.DataFrame | None = None,
            y_cal: np.ndarray | None = None) -> M1Model:
        self.categories_ = {c: sorted(x[c].astype(str).unique()) for c in CATEGORICAL}
        self.booster_ = lgb.LGBMClassifier(
            n_estimators=self.n_estimators, learning_rate=self.learning_rate, num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples, random_state=self.random_state, verbose=-1,
        ).fit(self._frame(x), y)
        self.calibrator_: IsotonicRegression | None = None
        if x_cal is not None and y_cal is not None:
            raw = self.booster_.predict_proba(self._frame(x_cal))[:, 1]
            self.calibrator_ = IsotonicRegression(out_of_bounds="clip").fit(raw, y_cal)
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        raw = self.booster_.predict_proba(self._frame(x))[:, 1]
        p = self.calibrator_.predict(raw) if self.calibrator_ is not None else raw
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return np.column_stack([1 - p, p])

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(x)[:, 1] >= 0.5).astype(int)


def baseline_model() -> Pipeline:
    return Pipeline([
        ("prep", ColumnTransformer([
            ("num", StandardScaler(), NUMERIC),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL),
        ])),
        ("lr", LogisticRegression(max_iter=2000, C=0.5)),
    ])


@dataclass
class M1Result:
    baseline: dict[str, float]
    advanced: dict[str, float]
    model: M1Model
    split: dict[str, list[int]]
    n_rows: dict[str, int]
    importance: dict[str, float] = field(default_factory=dict)


def temporal_split(feats: pd.DataFrame, valid_months: int = 2, test_months: int = 2) -> dict[str, pd.DataFrame]:
    months = sorted(feats.month.unique())
    usable = [m for m in months if m >= 1]  # month 0 has no history for anyone
    test = usable[-test_months:]
    valid = usable[-(test_months + valid_months):-test_months]
    train = [m for m in usable if m not in test and m not in valid]
    return {"train": feats[feats.month.isin(train)], "valid": feats[feats.month.isin(valid)],
            "test": feats[feats.month.isin(test)]}


def train(feats: pd.DataFrame) -> M1Result:
    parts = temporal_split(feats)
    tr, va, te = parts["train"], parts["valid"], parts["test"]
    base = baseline_model().fit(tr[NUMERIC + CATEGORICAL], tr.failed.to_numpy())
    base_metrics = classification_report(te.failed.to_numpy(), base.predict_proba(te[NUMERIC + CATEGORICAL])[:, 1])
    model = M1Model().fit(tr, tr.failed.to_numpy(), va, va.failed.to_numpy())
    adv_metrics = classification_report(te.failed.to_numpy(), model.predict_proba(te)[:, 1])
    gain = model.booster_.booster_.feature_importance(importance_type="gain")
    importance = dict(sorted(zip(NUMERIC + CATEGORICAL, (gain / gain.sum()).round(4), strict=True),
                             key=lambda kv: -kv[1]))
    return M1Result(base_metrics, adv_metrics, model,
                    {k: sorted(int(m) for m in v.month.unique()) for k, v in parts.items()},
                    {k: len(v) for k, v in parts.items()}, {k: float(v) for k, v in importance.items()})


def model_card(result: M1Result, passed: bool, reasons: list[str], provenance: dict[str, Any]) -> str:
    rows = "\n".join(f"| {k} | {result.baseline[k]:.4f} | {result.advanced[k]:.4f} |"
                     for k in ("auc", "pr_auc", "brier", "ece"))
    top = "\n".join(f"- `{k}`: {v:.3f}" for k, v in list(result.importance.items())[:8])
    return f"""# Model card — M1 Debit Failure Predictor

- **Purpose:** probability that a scheduled debit fails, predicted 3 days before execution, so the
  Pre-debit Guardian can intervene. Never used alone to take a money action.
- **Feature version:** `{FEATURE_VERSION}` (point-in-time; leakage test in tests/ml)
- **Data provenance:** {provenance}
- **IMPORTANT:** trained and evaluated on **synthetic RecurSim data**. These numbers show the pipeline
  works; they are **not** production performance.
- **Temporal split (months):** {result.split} · rows {result.n_rows}
- **Test base rate:** {result.advanced['base_rate']:.3f}

| metric (test) | baseline LR | LightGBM + isotonic |
|---|---|---|
{rows}

**Gate:** {"PASSED" if passed else "FAILED"} {"" if passed else reasons}

**Top features (gain share):**
{top}

**Known limitations:** no real customer data yet; advanced sequence model (TFT) deferred (ADR-0006);
per-tenant retraining required before any tenant use (ADR-0004 f).
**Monitoring:** PSI drift on inputs, calibration (ECE) on weekly labelled outcomes; retrain on drift or ECE > gate.
**Rollback:** re-point the `champion` alias to the previous registered version.
"""
