"""Per-tenant ML lifecycle on real services (Postgres + Iceberg/SeaweedFS + Redis + MLflow).

A planted-signal merchant: failure risk rises when the price increases and when the due date falls on a weekend.
Both vary cycle to cycle, so the prior (the subscription's own failure history) cannot see them but the feature
set can. This proves the pipeline LEARNS real signal and that a model only takes decisions after proving itself
on live verified outcomes — then rolls back when it degrades. (Synthetic data, labelled as such.)
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.data.pipeline import run_tenant
from nirantar.db.session import tenant_tx
from nirantar.features.compute import Cycle, TenantEvents, compute_features
from nirantar.features.definitions import LEAD
from nirantar.features.online import OnlineStore
from nirantar.ml import rollout
from nirantar.ml.monitoring import run_monitoring
from nirantar.ml.router import PRIOR_VERSION, ModelRouter
from nirantar.ml.tenant_training import should_train, train_m1

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(microsecond=0)


def p_fail(base: float, price_up: bool, weekend: bool) -> float:
    # strong enough that the oracle AUC (~0.78) sits well above the 0.65 gate: the test must not be borderline
    return min(0.95, base + (0.45 if price_up else 0.0) + (0.25 if weekend else 0.0))


class PlantedSource:
    provider = "simulated"

    def __init__(self, n_subs: int = 800, months: int = 12, seed: int = 3) -> None:
        rng = random.Random(seed)
        self.payments: list[dict[str, Any]] = []
        start = NOW - timedelta(days=30 * months + 2)
        for s in range(n_subs):
            base, amount = rng.uniform(0.02, 0.12), rng.choice([19900, 49900, 99900])
            hike = rng.randrange(2, months) if rng.random() < 0.6 else -1
            day0 = rng.randrange(0, 28)
            for m in range(months):
                due = start + timedelta(days=30 * m + day0, hours=6)
                price_up = m == hike
                if price_up:
                    amount = int(amount * 1.5)
                failed = rng.random() < p_fail(base, price_up, due.weekday() >= 5)
                pay = {"entity": "payment", "id": f"pay_{s}_{m}", "amount": amount, "currency": "INR", "method": "upi",
                       "bank": f"BANK{s % 5}", "customer_id": f"cust_{s}", "notes": {"subscription_id": f"sub_{s}"},
                       "created_at": int(due.timestamp()), "status": "failed" if failed else "captured"}
                if failed:
                    pay.update(error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds")
                self.payments.append(pay)
                if failed and rng.random() < 0.5:
                    self.payments.append({**pay, "id": f"pay_{s}_{m}_r", "status": "captured", "error_code": None,
                                          "error_reason": None,
                                          "created_at": int((due + timedelta(days=2)).timestamp())})

    def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        if entity != "payments":
            return iter(())
        return (p for p in self.payments if since.timestamp() <= p["created_at"] < until.timestamp())

    def invoices_for(self, subscription_id: str) -> Iterator[dict[str, Any]]:
        return iter(())


def _live_batch(engine: Engine, router: ModelRouter, tenant: str, n: int, *, seed: int, at: datetime,
                corrupt_labels: bool = False, amount_scale: float = 1.0) -> None:
    """Score n new debits with the router (as the workflow would) and record their verified outcomes."""
    rng = random.Random(seed)
    events = TenantEvents()
    with tenant_tx(tenant, engine) as c:
        for i in range(n):
            price_up, due = rng.random() < 0.4, at + LEAD + timedelta(days=rng.randrange(0, 7))
            hist = [Cycle(due - timedelta(days=30 * k), rng.random() < 0.07, "INSUFFICIENT_FUNDS", 49900,
                          due - timedelta(days=30 * k), False, None, "upi", "BANK1") for k in range(1, 7)]
            feats = compute_features(hist, as_of=at, due_at=due, amount_minor=int((74850 if price_up else 49900)
                                                                                * amount_scale),
                                     method="upi", bank="BANK1", tenant=events)
            subject = f"deb_live_{seed}_{i}"
            router.score(c, tenant, subject_id=subject, entity_id=f"sub_live_{i}", features=feats, now=at)
            failed = rng.random() < p_fail(0.07, price_up, due.weekday() >= 5)
            if corrupt_labels:
                failed = not price_up          # the world changed: price rises no longer predict failure
            c.execute(text("INSERT INTO ai.labels (tenant_id, label_id, prediction_id, subject_id, label_name, value, "
                           "source, observed_at) VALUES (:t, :l, NULL, :s, 'debit_failed', CAST(:v AS jsonb), "
                           "'verifier', :at)"),
                      {"t": tenant, "l": new_id("lbl"), "s": subject, "v": json.dumps({"value": failed}), "at": at})


def _stage(engine: Engine, tenant: str, version: str) -> str:
    with tenant_tx(tenant, engine) as c:
        return str(c.execute(text("SELECT stage FROM ai.model_versions WHERE tenant_id=:t AND version=:v"),
                             {"t": tenant, "v": version}).scalar_one())


def test_learn_shadow_canary_champion_rollback_and_drift(app_engine: Engine, lake: Any) -> None:
    tenant = new_id("ten")
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Planted-signal merchant (synthetic)", {"segments": ["subscription"],
                                                                         "synthetic": True})
    out = run_tenant(app_engine, lake, tenant, now=NOW, trigger="test", sources=[PlantedSource()],
                     backfill_since=NOW - timedelta(days=400))
    assert out["health"]["readiness"]["m1_debit_failure"]["ready"]
    assert should_train(app_engine, lake, tenant) == (True, "first model for this feature set")

    # ---- 1. offline: the learned model must beat the prior (it can see price rises and weekends)
    res = train_m1(app_engine, lake, tenant, now=NOW)
    assert res.status == "shadow", res.gate_failures
    assert res.metrics["auc"] > res.baseline["auc"] + 0.05 and res.metrics["brier"] < res.baseline["brier"]
    version = str(res.version)
    assert should_train(app_engine, lake, tenant)[0] is False            # no reason to retrain now

    # ---- 2. shadow: scored on every request, decides nothing
    router = ModelRouter()
    _live_batch(app_engine, router, tenant, 260, seed=1, at=NOW)
    with tenant_tx(tenant, app_engine) as c:
        roles = dict(c.execute(text("SELECT model_version, output->>'role' FROM ai.predictions WHERE tenant_id=:t "
                                    "AND subject_id='deb_live_1_0'"), {"t": tenant}).all())
    assert roles == {PRIOR_VERSION: "decision", version: "shadow"}

    # ---- 3. live evidence → canary → champion
    moves = rollout.evaluate_rollout(app_engine, tenant, now=NOW + timedelta(hours=1))
    assert [(m["from"], m["to"]) for m in moves] == [("shadow", "canary")], moves
    assert moves[0]["evidence"]["ci95"][1] <= 0.002
    _live_batch(app_engine, router, tenant, 260, seed=2, at=NOW + timedelta(hours=2))
    moves = rollout.evaluate_rollout(app_engine, tenant, now=NOW + timedelta(hours=3))
    assert [(m["from"], m["to"]) for m in moves] == [("canary", "champion")], moves
    with tenant_tx(tenant, app_engine) as c:
        dec = router.score(c, tenant, subject_id="deb_check", entity_id="sub_any",
                           features=compute_features([], as_of=NOW, due_at=NOW + LEAD, amount_minor=49900,
                                                     method="upi", bank="BANK1", tenant=TenantEvents()), now=NOW)
    assert dec.served_by == "champion" and dec.version == version

    # ---- 4. monitoring: served amounts shift 3× → drift detected → retrain trigger
    _live_batch(app_engine, router, tenant, 150, seed=3, at=NOW + timedelta(hours=4), amount_scale=3.0)
    rep = run_monitoring(app_engine, lake, tenant, now=NOW + timedelta(hours=5))
    assert rep["drift"] is not None and rep["drift"]["columns"]["log_amount"]["drifted"]
    assert rep["performance"][version]["n"] > 500

    # ---- 5. the world changes (labels contradict the model) → automatic rollback to the prior
    _live_batch(app_engine, router, tenant, 520, seed=4, at=NOW + timedelta(hours=6), corrupt_labels=True)
    moves = rollout.evaluate_rollout(app_engine, tenant, now=NOW + timedelta(hours=7))
    assert [(m["from"], m["to"]) for m in moves] == [("champion", "retired")], moves
    assert _stage(app_engine, tenant, version) == "retired"
    with tenant_tx(tenant, app_engine) as c:
        events = [tuple(r) for r in c.execute(text(
            "SELECT from_stage, to_stage FROM ai.model_events WHERE tenant_id=:t ORDER BY at"), {"t": tenant})]
    assert events == [(None, "shadow"), ("shadow", "canary"), ("canary", "champion"), ("champion", "retired")]
    assert "champion was rolled back" in run_monitoring(app_engine, lake, tenant,
                                                        now=NOW + timedelta(hours=8))["triggers"]


def test_online_features_equal_offline_training_features(app_engine: Engine, lake: Any) -> None:
    """Parity: the online store + the shared function reproduce the training row for the latest cycle."""
    from nirantar.features.offline import build_training_set

    tenant = new_id("ten")
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Parity merchant (synthetic)", {"segments": ["subscription"], "synthetic": True})
    run_tenant(app_engine, lake, tenant, now=NOW, trigger="test", sources=[PlantedSource(n_subs=30, months=6)],
               backfill_since=NOW - timedelta(days=400))
    ts = build_training_set(lake, tenant, now=NOW, source="synthetic")
    store = OnlineStore()
    at = ts.as_of.max().to_pydatetime() + timedelta(hours=1)   # materialise "just after" the latest prediction time
    store.materialize(lake, tenant, now=at)
    last = ts.sort_values("as_of").groupby("entity_id").tail(1).head(10)
    from nirantar.features.definitions import CATEGORICAL, NUMERIC

    for r in last.itertuples():
        online = store.features_for(tenant, r.entity_id, as_of=r.as_of.to_pydatetime(),
                                    due_at=r.due_at.to_pydatetime(), amount_minor=round(math.exp(r.log_amount)),
                                    method=r.method, bank=r.bank).row
        for f in NUMERIC:
            if f in ("tenant_fail_rate_30d", "tenant_technical_rate_24h", "tenant_technical_rate_7d",
                     "bank_technical_rate_24h", "bank_technical_rate_7d", "smoothed_fail_rate"):
                continue    # tenant windows: online keeps 31 days of events; compared separately below
            assert online[f] == pytest.approx(getattr(r, f), abs=1e-6), (f, r.entity_id)
        for f in CATEGORICAL:
            assert online[f] == getattr(r, f)
    # for an as_of inside the online events window, tenant-level features match exactly too
    recent = ts[ts.as_of >= at - timedelta(days=1)].head(5)
    assert len(recent) > 0
    for r in recent.itertuples():
        online = store.features_for(tenant, r.entity_id, as_of=r.as_of.to_pydatetime(),
                                    due_at=r.due_at.to_pydatetime(), amount_minor=round(math.exp(r.log_amount)),
                                    method=r.method, bank=r.bank).row
        assert online["tenant_fail_rate_30d"] == pytest.approx(r.tenant_fail_rate_30d, abs=1e-9)
        assert online["smoothed_fail_rate"] == pytest.approx(r.smoothed_fail_rate, abs=1e-9)
