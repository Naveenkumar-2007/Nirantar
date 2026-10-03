"""Daily churn scoring (sBG baseline through the router) and delayed labelling from the lifecycle table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import NewCustomer, connect_provider, create_customer, create_subscription, create_tenant
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.data.lifecycle import build_lifecycle
from nirantar.data.pipeline import run_tenant
from nirantar.data.sources import MockSource
from nirantar.db.session import tenant_tx
from nirantar.features.online import OnlineStore
from nirantar.ml.router import ModelRouter
from nirantar.payments.providers.mock import MockProvider
from nirantar.retention.fit import fit_retention
from nirantar.retention.scoring import label_predictions, score_active

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(microsecond=0)


def test_active_subscribers_are_scored_then_labelled_when_the_outcome_is_known(app_engine: Engine, lake: Any) -> None:
    tenant, mock = new_id("ten"), MockProvider()
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Scoring merchant (synthetic)", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, tenant, "mock", "test", "literal:x", "literal:y")
        for i in range(60):
            cust = create_customer(c, tenant, NewCustomer(f"s{i}", f"C {i}", None, None, "en"))
            psub = mock.add_subscription(cust, Money.of("299"))
            create_subscription(c, tenant, cust, "mock", psub, Money.of("299"))
            active = i % 2 == 0
            last = 10 if active else 60 + (i % 7)
            for k in range(4 + i % 5):
                mock.charge(psub, succeed=not (not active and i % 4 == 1 and k == 0), at=NOW - timedelta(
                    days=last + 30 * k))
    run_tenant(app_engine, lake, tenant, now=NOW, trigger="test", sources=[MockSource(mock)],
               backfill_since=NOW - timedelta(days=400))
    fit = fit_retention(app_engine, lake, tenant, now=NOW)
    assert "sbg" in fit and fit["subscribers"] == 60

    store = OnlineStore()
    store.materialize(lake, tenant, now=NOW)
    rep = score_active(app_engine, lake, tenant, now=NOW, store=store, router=ModelRouter())
    assert rep["active"] == 30 and rep["scored"] == 30 and rep["unscored"] == 0
    assert rep["decided_by"] == ["sbg-prior"]                       # no trained model: the transparent baseline
    assert 0 < rep["average"] < 1 and rep["threshold"] >= rep["average"]
    again = score_active(app_engine, lake, tenant, now=NOW, store=store, router=ModelRouter())
    assert again["scored"] == 30                                    # idempotent: same-day re-run reuses scores
    with tenant_tx(tenant, app_engine) as c:
        n_preds = c.execute(text("SELECT count(*) FROM ai.predictions WHERE tenant_id=:t AND model_name='m6_churn' "
                                 "AND output->>'role'='decision'"), {"t": tenant}).scalar_one()
    assert n_preds == 30

    # nothing observable yet → no labels
    assert label_predictions(app_engine, lake, tenant, now=NOW) == {"churned_60d": 0, "churn_involuntary": 0}
    # 70 days later nobody paid again: every scored subscriber lapsed within the 60-day window
    later = NOW + timedelta(days=70)
    build_lifecycle(lake, tenant, now=later)
    written = label_predictions(app_engine, lake, tenant, now=later)
    assert written == {"churned_60d": 30, "churn_involuntary": 30}
    with tenant_tx(tenant, app_engine) as c:
        vals = dict(c.execute(text("SELECT label_name, bool_and((value->>'value')::boolean) FROM ai.labels WHERE "
                                   "tenant_id=:t AND source='lifecycle' GROUP BY 1"), {"t": tenant}).all())
    assert vals == {"churned_60d": True, "churn_involuntary": False}   # they left after paying: by choice
    assert label_predictions(app_engine, lake, tenant, now=later) == {"churned_60d": 0, "churn_involuntary": 0}
