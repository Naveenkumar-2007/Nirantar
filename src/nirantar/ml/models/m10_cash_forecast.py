"""M10 Cash Forecaster: expected collections per day with a prediction interval.

Baseline : naive — same weekday last week's actual collections.
Advanced : Σ amount × (1 − P_fail from M1) for debits executing that day, plus
           split-conformal intervals from validation residuals (nominal 90%).
Metrics  : WAPE on test days, empirical interval coverage.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from nirantar.ml.metrics import wape


@dataclass(frozen=True)
class ForecastReport:
    baseline: dict[str, float]
    advanced: dict[str, float]
    days: int


def daily_frame(feats: pd.DataFrame, p_fail: np.ndarray) -> pd.DataFrame:
    df = feats[["scheduled_for", "amount_minor", "failed"]].copy()
    df["expected"] = df.amount_minor * (1 - p_fail)
    df["actual"] = df.amount_minor * (1 - df.failed)
    return df.groupby("scheduled_for", as_index=False)[["expected", "actual"]].sum().sort_values("scheduled_for")


def evaluate(valid_daily: pd.DataFrame, test_daily: pd.DataFrame, alpha: float = 0.10) -> ForecastReport:
    # conformal: absolute residual quantile from validation days, applied to test days
    resid = (valid_daily.actual - valid_daily.expected).abs().to_numpy(dtype=float)
    n = len(resid)
    q = float(np.quantile(resid, min(1.0, np.ceil((n + 1) * (1 - alpha)) / n))) if n else 0.0
    actual = test_daily.actual.to_numpy(dtype=float)
    adv = test_daily.expected.to_numpy(dtype=float)
    coverage = float(np.mean((actual >= adv - q) & (actual <= adv + q)))
    both = pd.concat([valid_daily, test_daily]).sort_values("scheduled_for").set_index("scheduled_for")
    lag7 = both.actual.shift(7).reindex(test_daily.scheduled_for).to_numpy(dtype=float)
    ok = ~np.isnan(lag7)
    return ForecastReport(
        baseline={"wape": wape(actual[ok], lag7[ok])},
        advanced={"wape": wape(actual, adv), "interval_coverage": coverage, "interval_halfwidth_minor": q},
        days=len(test_daily),
    )
