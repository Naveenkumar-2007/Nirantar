"""Fast model-level tests on small simulations (full evaluation lives in nirantar.ml.pipelines)."""

from __future__ import annotations

import numpy as np
import pytest

from nirantar.ml import registry
from nirantar.ml.features import build_features
from nirantar.ml.metrics import expected_calibration_error, qini_auc, wape
from nirantar.ml.models import m1_debit_failure as m1
from nirantar.ml.models import m4_bank_health as m4
from nirantar.ml.models import m5_uplift as m5
from nirantar.ml.recursim import SimConfig, SimResult, simulate


@pytest.fixture(scope="module")
def sim() -> SimResult:
    return simulate(SimConfig(n_customers=1200, months=10, seed=5))


def test_m1_learns_signal_and_is_calibrated(sim: SimResult) -> None:
    feats = build_features(sim.customers, sim.debits, sim.failures, sim.bank_hourly)
    res = m1.train(feats)
    assert res.advanced["auc"] > 0.65
    assert res.advanced["ece"] < 0.06
    assert set(res.split["test"]).isdisjoint(res.split["train"])
    assert max(res.split["train"]) < min(res.split["test"])  # temporal, not random


def test_m4_detects_severe_outages(sim: SimResult) -> None:
    ewma, bocpd = m4.run(sim.bank_hourly)
    assert ewma.incident_recall > 0.8 and bocpd.incident_recall > 0.6
    assert bocpd.false_alarms_per_bank_week <= 2.0


def test_m5_policies_are_evaluated_on_true_outcomes(sim: SimResult) -> None:
    df = m5.uplift_frame(sim.customers, sim.debits, sim.failures)
    train, test = df[df.month < 7], df[df.month >= 7]
    cap = m5.evaluate_capacity(train, test, dict(zip(m5.ARMS, sim.config.arm_probs, strict=True)))
    assert cap["oracle"] >= max(v for k, v in cap.items() if k != "oracle") - 1e-6
    assert cap["random"] <= cap["oracle"]


def test_metrics_sanity() -> None:
    y = np.array([0, 0, 1, 1])
    assert expected_calibration_error(y, np.array([0.0, 0.0, 1.0, 1.0])) == 0.0
    assert wape(np.array([100.0, 100.0]), np.array([90.0, 110.0])) == pytest.approx(0.1)
    rng = np.random.default_rng(0)
    t = rng.random(4000) < 0.5
    x = rng.random(4000)
    y = rng.random(4000) < (0.3 + 0.4 * t * x)  # effect grows with x
    assert qini_auc(y, t, x) > qini_auc(y, t, -x)


def test_gate_logic() -> None:
    ok, _ = registry.evaluate_gates("m1_debit_failure", {"auc": 0.8, "ece": 0.01, "pr_auc": 0.5, "brier": 0.1},
                                    {"pr_auc": 0.4, "brier": 0.2})
    bad, reasons = registry.evaluate_gates("m1_debit_failure", {"auc": 0.6, "ece": 0.2, "pr_auc": 0.3, "brier": 0.3},
                                           {"pr_auc": 0.4, "brier": 0.2})
    assert ok and not bad and len(reasons) == 4
