"""Per-tenant M1 models on feature set subscription_payment:v1 (P3, ADR-0013).

Candidates, all trained on the tenant's own point-in-time training set:
  PriorModel   — cold start and baseline: the subscription's smoothed failure rate (no training needed)
  LogitModel   — logistic regression (scaled numeric + one-hot categorical)
  GBMModel     — LightGBM + isotonic calibration on the validation window
"""

from __future__ import annotations

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

from nirantar.features.definitions import CATEGORICAL, FEATURE_SET_ID, NUMERIC

MODEL_NAME = "m1_debit_failure"
TRUSTED_TYPES = [
    "collections.OrderedDict", "lightgbm.basic.Booster", "lightgbm.sklearn.LGBMClassifier",
    "nirantar.ml.models.m1_tenant.GBMModel", "nirantar.ml.models.m1_tenant.PriorModel",
    "sklearn.isotonic.IsotonicRegression", "nirantar.ml.models.m1_tenant.Calibrated",
]


def _proba(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.column_stack([1 - p, p])


class PriorModel(BaseEstimator, ClassifierMixin):
    """Transparent cold-start model: P(fail) = (fails + k·tenant rate) / (n + k) for this subscription."""
    feature_set = FEATURE_SET_ID
    algorithm = "prior"

    def fit(self, x: pd.DataFrame, y: np.ndarray) -> PriorModel:
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        return _proba(x["smoothed_fail_rate"].to_numpy(dtype=float))

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(x)[:, 1] >= 0.5).astype(int)


def logit_model() -> Pipeline:
    return Pipeline([
        ("prep", ColumnTransformer([("num", StandardScaler(), NUMERIC),
                                    ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=20), CATEGORICAL)])),
        ("lr", LogisticRegression(max_iter=3000, C=0.5)),
    ])


class Calibrated(BaseEstimator, ClassifierMixin):
    """A fitted scorer + isotonic calibration fitted on a separate calibration window."""

    def __init__(self, base: Any, algorithm: str) -> None:
        self.base = base
        self.algorithm = algorithm

    feature_set = FEATURE_SET_ID

    def calibrate(self, x_cal: pd.DataFrame, y_cal: np.ndarray) -> Calibrated:
        raw = self.base.predict_proba(x_cal[NUMERIC + CATEGORICAL])[:, 1]
        self.calibrator_ = IsotonicRegression(out_of_bounds="clip").fit(raw, y_cal)
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        return _proba(self.calibrator_.predict(self.base.predict_proba(x[NUMERIC + CATEGORICAL])[:, 1]))

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(x)[:, 1] >= 0.5).astype(int)


class GBMModel(BaseEstimator, ClassifierMixin):
    feature_set = FEATURE_SET_ID
    algorithm = "lightgbm+isotonic"

    def __init__(self, n_estimators: int = 300, learning_rate: float = 0.05, num_leaves: int = 15,
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
            y_cal: np.ndarray | None = None) -> GBMModel:
        self.categories_ = {c: sorted(x[c].astype(str).unique()) for c in CATEGORICAL}
        self.booster_ = lgb.LGBMClassifier(
            n_estimators=self.n_estimators, learning_rate=self.learning_rate, num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples, random_state=self.random_state, verbose=-1,
        ).fit(self._frame(x), y)
        self.calibrator_: IsotonicRegression | None = None
        if x_cal is not None and y_cal is not None and len(np.unique(y_cal)) == 2:
            raw = self.booster_.predict_proba(self._frame(x_cal))[:, 1]
            self.calibrator_ = IsotonicRegression(out_of_bounds="clip").fit(raw, y_cal)
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        raw = self.booster_.predict_proba(self._frame(x))[:, 1]
        return _proba(self.calibrator_.predict(raw) if self.calibrator_ is not None else raw)

    def importance(self) -> dict[str, float]:
        gain = self.booster_.booster_.feature_importance(importance_type="gain")
        total = gain.sum() or 1.0
        return dict(sorted(((n, round(float(g / total), 4)) for n, g in zip(NUMERIC + CATEGORICAL, gain,
                                                                             strict=True)), key=lambda kv: -kv[1]))
