"""Churn models on the shared feature set (P4, ADR-0014).

M6  churn within HORIZON (60 days) of a prediction time. Rows = the feature-store snapshots of every billing cycle
    (gold.training_m1: features at as_of = due − 3 days); label = the subscription churned in (as_of, as_of + 60d].
    Right-censoring: a row is kept only if its outcome is observable (churned by as_of + 60d, or as_of + 60d ≤ now).
    Transparent baseline / cold-start prior: the tenant's sBG tenure model — P(no renewal within the next
    ⌈60 / period⌉ periods | periods paid). For offline gating it is fitted ONLY on what was known at the training
    cutoff, so the baseline cannot see the test period.
M13 churn type: among churned subscriptions, P(payment-driven / involuntary) from the snapshot before their last
    cycle. Baseline: a calibrated rule — share of involuntary churn in the tenant's training rows, split by
    "any recent payment trouble" (last failed, a failure streak, or funds failures in the last 6 cycles).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
from sklearn.base import BaseEstimator, ClassifierMixin

from nirantar.data.lake import Lake
from nirantar.data.lifecycle import TABLE as LIFECYCLE
from nirantar.features.compute import tenure_stats
from nirantar.features.definitions import CATEGORICAL, FEATURE_SET_ID, NUMERIC
from nirantar.features.offline import TRAINING_TABLE as M1_TABLE
from nirantar.features.offline import cycles_with_entities, to_cycle
from nirantar.ml.clv import SBGFit, fit_sbg

HORIZON = timedelta(days=60)
M6_TABLE = "gold.training_m6"
M13_TABLE = "gold.training_m13"
EXTRA = ["paid_periods", "period_days"]


def _proba(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.column_stack([1 - p, p])


class SBGChurnPrior(BaseEstimator, ClassifierMixin):
    """P(churn within horizon) = 1 − Π_{i<k} P(renew | paid n+i), k = ⌈horizon / period⌉ (sBG, tenure only)."""
    algorithm = "sbg-tenure-prior"

    def __init__(self, alpha: float, beta: float, horizon_days: float = HORIZON.days) -> None:
        self.alpha = alpha
        self.beta = beta
        self.horizon_days = horizon_days
        self.classes_ = np.array([0, 1])

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        fit = SBGFit(self.alpha, self.beta, 0.0, 0, 0)
        out = []
        for n, period in zip(x["paid_periods"].to_numpy(float), x["period_days"].to_numpy(float), strict=True):
            k, s = max(1, math.ceil(self.horizon_days / max(period, 1.0))), 1.0
            for i in range(k):
                s *= fit.p_renew(max(1, int(n)) + i)
            out.append(1 - s)
        return _proba(np.array(out))

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(x)[:, 1] >= 0.5).astype(int)


def payment_trouble(x: pd.DataFrame) -> np.ndarray:
    return ((x["last_failed"] > 0) | (x["consecutive_failures"] > 0) | (x["fails_funds_last6"] > 0)).to_numpy()


class RuleTypePrior(BaseEstimator, ClassifierMixin):
    """Calibrated rule for churn type: involuntary share by 'recent payment trouble' bucket (Laplace-smoothed)."""
    algorithm = "payment-trouble-rule"

    def __init__(self, p_trouble: float = 0.5, p_clean: float = 0.5) -> None:
        self.p_trouble = p_trouble
        self.p_clean = p_clean
        self.classes_ = np.array([0, 1])

    def fit(self, x: pd.DataFrame, y: np.ndarray) -> RuleTypePrior:
        t = payment_trouble(x)
        self.p_trouble = (y[t].sum() + 1) / (t.sum() + 2)
        self.p_clean = (y[~t].sum() + 1) / ((~t).sum() + 2)
        return self

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        return _proba(np.where(payment_trouble(x), self.p_trouble, self.p_clean))

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(x)[:, 1] >= 0.5).astype(int)


def _with_tenure(lake: Lake, tenant_id: str, rows: pd.DataFrame) -> pd.DataFrame:
    """Add the sBG prior's inputs (paid_periods, period_days) to snapshot rows, from each entity's history."""
    co = cycles_with_entities(lake, tenant_id)
    hist = {str(e): [to_cycle(r) for r in g.itertuples()] for e, g in co.groupby("entity_id")} if not co.empty else {}
    stats = [tenure_stats(hist.get(str(r.entity_id), []), as_of=r.as_of.to_pydatetime()) for r in rows.itertuples()]
    return rows.assign(paid_periods=[s["paid_periods"] for s in stats], period_days=[s["period_days"] for s in stats])


def _write(lake: Lake, table: str, tenant_id: str, df: pd.DataFrame) -> None:
    schema = pa.schema(
        [("tenant_id", pa.string()), ("cycle_id", pa.string()), ("entity_id", pa.string()),
         ("as_of", pa.timestamp("us", tz="UTC")), ("due_at", pa.timestamp("us", tz="UTC")), ("label", pa.bool_()),
         ("feature_set", pa.string()), ("source", pa.string())]
        + [(c, pa.float64()) for c in NUMERIC + EXTRA] + [(c, pa.string()) for c in CATEGORICAL])
    lake.replace_tenant(table, tenant_id, pa.Table.from_pandas(df[schema.names], schema=schema, preserve_index=False))


def build_m6_training_set(lake: Lake, tenant_id: str, *, now: datetime, source: str) -> pd.DataFrame:
    snaps = lake.read(M1_TABLE, tenant_id)
    lc = lake.read(LIFECYCLE, tenant_id)
    if snaps.empty or lc.empty:
        return pd.DataFrame()
    m = snaps.drop(columns=["label"]).merge(lc[["entity_id", "status", "churn_at"]], on="entity_id", how="inner")
    churned = m.status == "churned"
    in_window = churned & (m.churn_at > m.as_of) & (m.churn_at <= m.as_of + HORIZON)
    observable = (churned & (m.churn_at <= m.as_of + HORIZON)) | (m.as_of + HORIZON <= pd.Timestamp(now))
    keep = observable & ~(churned & (m.churn_at <= m.as_of))            # never predict after the churn happened
    df = m[keep].assign(label=in_window[keep], feature_set=FEATURE_SET_ID, source=source)
    df = _with_tenure(lake, tenant_id, df.drop(columns=["status", "churn_at"]).reset_index(drop=True))
    _write(lake, M6_TABLE, tenant_id, df)
    return df.sort_values("as_of").reset_index(drop=True)


def build_m13_training_set(lake: Lake, tenant_id: str, *, now: datetime, source: str) -> pd.DataFrame:
    snaps = lake.read(M1_TABLE, tenant_id)
    lc = lake.read(LIFECYCLE, tenant_id)
    if snaps.empty or lc.empty:
        return pd.DataFrame()
    churned = lc[(lc.status == "churned") & lc.churn_type.isin(["involuntary", "voluntary"])]
    last = snaps.sort_values("as_of").groupby("entity_id").tail(1)       # snapshot before the last cycle
    m = last.drop(columns=["label"]).merge(churned[["entity_id", "churn_type"]], on="entity_id", how="inner")
    df = m.assign(label=m.churn_type == "involuntary", feature_set=FEATURE_SET_ID, source=source)
    df = _with_tenure(lake, tenant_id, df.drop(columns=["churn_type"]).reset_index(drop=True))
    _write(lake, M13_TABLE, tenant_id, df)
    return df.sort_values("as_of").reset_index(drop=True)


def sbg_prior_as_of(lake: Lake, tenant_id: str, cutoff: datetime) -> SBGChurnPrior | None:
    """sBG fitted only on what was known at `cutoff` (paid periods and observed churn before it)."""
    co = cycles_with_entities(lake, tenant_id)
    lc = lake.read(LIFECYCLE, tenant_id)
    if co.empty or lc.empty:
        return None
    churn_at = dict(zip(lc.entity_id, lc.churn_at, strict=True))
    status = dict(zip(lc.entity_id, lc.status, strict=True))
    paid, churned = [], []
    for e, g in co.groupby("entity_id"):
        n = int(((g.paid_at.notna()) & (g.paid_at < pd.Timestamp(cutoff))).sum())
        if n < 1:
            continue
        ca = churn_at.get(str(e))
        paid.append(n)
        churned.append(status.get(str(e)) == "churned" and ca is not None and not pd.isna(ca)
                       and ca <= pd.Timestamp(cutoff))
    if len(paid) < 30 or sum(churned) < 5:
        return None
    fit = fit_sbg(np.array(paid), np.array(churned))
    return SBGChurnPrior(fit.alpha, fit.beta)


def baseline_m6(lake: Lake, tenant_id: str, train: pd.DataFrame, y: np.ndarray) -> Any:
    prior = sbg_prior_as_of(lake, tenant_id, train.as_of.max().to_pydatetime())
    if prior is None:
        raise ValueError("not enough churn history before the training cutoff to fit the sBG baseline")
    return prior


def baseline_m13(lake: Lake, tenant_id: str, train: pd.DataFrame, y: np.ndarray) -> Any:
    return RuleTypePrior().fit(train, y)
