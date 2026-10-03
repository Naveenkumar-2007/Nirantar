from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from nirantar.ml.cash_window import circular_distance, estimate
from nirantar.ml.features import build_features
from nirantar.ml.recursim import SimConfig, SimResult, simulate


@pytest.fixture(scope="module")
def sim() -> SimResult:
    return simulate(SimConfig(n_customers=400, months=8, seed=11))


def test_simulation_is_deterministic_per_seed() -> None:
    a = simulate(SimConfig(n_customers=60, months=3, seed=3))
    b = simulate(SimConfig(n_customers=60, months=3, seed=3))
    pd.testing.assert_frame_equal(a.debits, b.debits)
    pd.testing.assert_frame_equal(a.failures, b.failures)


def test_counterfactuals_are_consistent(sim: SimResult) -> None:
    f = sim.failures
    # the observed outcome equals the potential outcome of the assigned arm
    observed = f.apply(lambda r: r[f"cf_{r.arm}"], axis=1)
    assert (observed == f.recovered).all()
    # potential outcomes share one draw: a higher probability arm can't do worse
    for arm in ("whatsapp", "voice"):
        better = f[f[f"p_{arm}"] >= f.p_none]
        assert (better[f"cf_{arm}"] >= better.cf_none).all()
    assert np.allclose(f.tau_voice, f.p_voice - f.p_none)


def test_everything_is_labelled_synthetic(sim: SimResult) -> None:
    for df in (sim.customers, sim.debits, sim.failures, sim.bank_hourly):
        assert df.attrs["source"] == "recursim"


def test_cash_window_estimator() -> None:
    cw = estimate([1, 2, 30, 1], [1, 1, 1, 1])
    assert cw.day is not None and circular_distance(cw.day, 1) == pytest.approx(0.25, abs=1.0)
    assert cw.confidence > 0.9
    assert estimate([], []).day is None
    spread = estimate([1, 8, 15, 23], [1, 1, 1, 1])
    assert spread.confidence < 0.2


def test_features_have_no_future_leakage(sim: SimResult) -> None:
    feats = build_features(sim.customers, sim.debits, sim.failures, sim.bank_hourly)
    target = feats[feats.month == 5].iloc[0]
    cutoff = pd.Timestamp(target.scheduled_for) - timedelta(days=3)
    # rewrite everything that happened at/after the cutoff for this customer and the bank stream
    debits = sim.debits.copy()
    future = (debits.customer_id == target.customer_id) & (pd.to_datetime(debits.executed_at) >= cutoff)
    debits.loc[future, "succeeded"] = ~debits.loc[future, "succeeded"]
    debits.loc[future, "failure_code"] = "BANK_TECHNICAL"
    hourly = sim.bank_hourly.copy()
    hourly.loc[hourly.hour >= cutoff, "technical_failures"] = hourly.loc[hourly.hour >= cutoff, "attempts"]
    failures = sim.failures.copy()
    failures["recovered"] = np.where(failures.debit_id.isin(debits.loc[future, "debit_id"]),
                                     ~failures.recovered, failures.recovered)
    mutated = build_features(sim.customers, debits, failures, hourly)
    before = feats.set_index("debit_id").loc[target.debit_id]
    after = mutated.set_index("debit_id").loc[target.debit_id]
    feature_cols = [c for c in feats.columns if c not in ("debit_id", "failed", "failure_code")]
    pd.testing.assert_series_equal(before[feature_cols], after[feature_cols], check_names=False)
