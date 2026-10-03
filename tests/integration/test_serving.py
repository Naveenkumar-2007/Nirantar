from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.ml.features import build_features
from nirantar.ml.models import m1_debit_failure as m1
from nirantar.ml.recursim import SimConfig, simulate
from nirantar.ml.serving import Predictor

pytestmark = pytest.mark.integration


def test_predictions_are_persisted_with_version_and_feature_hash(app_engine: Engine) -> None:
    sim = simulate(SimConfig(n_customers=300, months=8, seed=2))
    feats = build_features(sim.customers, sim.debits, sim.failures, sim.bank_hourly)
    model = m1.train(feats).model
    predictor = Predictor(model, m1.MODEL_NAME, "test-1")
    t = new_id("ten")
    rows = feats[feats.month == 7].head(5)
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "x")
        preds = predictor.predict_and_log(c, t, rows, "debit_id", datetime.now(UTC))
        stored = c.execute(text("SELECT model_version, features_hash, score FROM ai.predictions")).all()
    assert len(preds) == len(stored) == 5
    assert all(0 < p.score < 1 for p in preds)
    assert {s.model_version for s in stored} == {"test-1"}
    with pytest.raises(ValueError, match="serving contract"):
        with tenant_tx(t, app_engine) as c:
            predictor.predict_and_log(c, t, rows.drop(columns=["bank_td_rate_24h"]), "debit_id", datetime.now(UTC))


def test_champion_loads_from_registry_when_trained() -> None:
    try:
        predictor = Predictor.from_registry(m1.MODEL_NAME)
    except Exception as exc:  # registry not populated in a fresh checkout
        pytest.skip(f"no champion registered yet (run nirantar.ml.pipelines): {type(exc).__name__}")
    assert predictor.model_version
