"""Per-tenant training for M1 (P3, ADR-0013). Only on the tenant's own data, only when the data is ready.

1. Readiness gate (data/readiness.yaml via the latest data-health report) — no model is trained on too little data.
2. Point-in-time training set (features/offline.py → gold.training_m1).
3. Temporal split by prediction time: 60% train · 10% calibration · 10% selection · 20% test (all disjoint, in
   time order — calibration is never evaluated on the rows it was fitted on).
4. Candidates: prior (cold start, the baseline), logistic regression, LightGBM — each learned model isotonic-
   calibrated on the calibration window; the better on selection-window Brier is tested against the prior on the
   untouched test window.
5. Offline gates (ml/gates.yaml: m1_debit_failure_tenant) incl. "must beat the prior". Passed → registered in
   MLflow as `m1_debit_failure.<tenant>` and enters SHADOW (never decides until it proves itself live);
   failed → recorded as rejected with reasons. Every result is written to ai.model_versions + ai.model_events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from nirantar.audit.chain import AuditChain
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.data import health
from nirantar.data.lake import Lake
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlAuditStore
from nirantar.features.definitions import CATEGORICAL, FEATURE_SET_ID, NUMERIC
from nirantar.ml import registry
from nirantar.ml.metrics import classification_report
from nirantar.ml.models import m1_tenant


@dataclass
class TrainResult:
    status: str                              # not_ready | rejected | shadow
    reason: str
    version: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    candidates: dict[str, dict[str, float]] = field(default_factory=dict)
    gate_failures: list[str] = field(default_factory=list)


def registry_name(tenant_id: str, model: str = m1_tenant.MODEL_NAME) -> str:
    return f"{model}.{tenant_id}"


def temporal_split(ts: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ts = ts.sort_values("as_of").reset_index(drop=True)
    a, b, c = int(len(ts) * 0.6), int(len(ts) * 0.7), int(len(ts) * 0.8)
    return {"train": ts.iloc[:a], "calibration": ts.iloc[a:b], "selection": ts.iloc[b:c], "test": ts.iloc[c:]}


def paired_brier_ci(y: np.ndarray, p_new: np.ndarray, p_old: np.ndarray, n_boot: int = 1000,
                    seed: int = 7) -> tuple[float, float, float]:
    """Mean and 95% bootstrap CI of Brier(new) − Brier(old) on the same rows (negative = new is better)."""
    d = (p_new - y) ** 2 - (p_old - y) ** 2
    rng = np.random.default_rng(seed)
    boots = [float(d[rng.integers(0, len(d), len(d))].mean()) for _ in range(n_boot)]
    return float(d.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def _data_source(engine: Engine, tenant_id: str) -> str:
    with tenant_tx(tenant_id, engine) as c:
        s: Any = c.execute(text("SELECT settings FROM core.tenants WHERE tenant_id=:t"),
                           {"t": tenant_id}).scalar_one()
    s = s if isinstance(s, dict) else json.loads(s or "{}")
    return "synthetic" if s.get("synthetic") or s.get("demo") else "tenant"


def train_m1(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime,
             actor: str = "system:training") -> TrainResult:
    from nirantar.ml.specs import M1

    return train_model(M1, engine, lake, tenant_id, now=now, actor=actor)


def train_model(sp: Any, engine: Engine, lake: Lake, tenant_id: str, *, now: datetime,
                actor: str = "system:training") -> TrainResult:
    """Generic per-tenant training for any ModelSpec (M1, M6, M13, ...): readiness -> training set -> temporal split
    -> candidates vs the spec's transparent baseline -> offline gates -> MLflow + ai.model_versions."""
    rep = health.latest(engine, tenant_id)
    readiness = (rep or {}).get("readiness", {}).get(sp.readiness_key)
    if not readiness or not readiness["ready"]:
        return TrainResult("not_ready", f"not enough data: {readiness['gaps'] if readiness else 'no report yet'}")
    source = _data_source(engine, tenant_id)
    ts = sp.build(lake, tenant_id, now, source)
    if ts.empty or ts.label.nunique() < 2:
        return TrainResult("not_ready", "training set empty or has a single class")
    parts = temporal_split(ts)
    tr, ca, se, te = parts["train"], parts["calibration"], parts["selection"], parts["test"]
    y_tr, y_ca, y_se, y_te = (p.label.astype(int).to_numpy() for p in (tr, ca, se, te))
    x_cols = NUMERIC + CATEGORICAL
    try:
        prior = sp.baseline(lake, tenant_id, tr, y_tr)
    except ValueError as exc:
        return TrainResult("not_ready", f"baseline unavailable: {exc}")

    def x(df: pd.DataFrame) -> pd.DataFrame:           # features + any inputs the prior needs (e.g. tenure)
        return df[[c for c in df.columns if c in set(x_cols) | {"paid_periods", "period_days"}]]

    logit = m1_tenant.Calibrated(m1_tenant.logit_model().fit(tr[x_cols], y_tr), "logit+isotonic").calibrate(ca, y_ca)
    gbm_base = m1_tenant.GBMModel().fit(tr, y_tr)
    gbm = m1_tenant.Calibrated(gbm_base, "lightgbm+isotonic").calibrate(ca, y_ca)
    valid = {name: classification_report(y_se, m.predict_proba(x(se))[:, 1])
             for name, m in (("prior", prior), ("logit", logit), ("lightgbm", gbm))}
    best_name = min(("logit", "lightgbm"), key=lambda n: valid[n]["brier"])
    best: Any = gbm if best_name == "lightgbm" else logit
    p_best, p_prior = best.predict_proba(x(te))[:, 1], prior.predict_proba(x(te))[:, 1]
    metrics = {**classification_report(y_te, p_best), "test_positives": float(y_te.sum())}
    baseline: dict[str, Any] = {**classification_report(y_te, p_prior),
                                "algorithm": getattr(prior, "algorithm", "prior")}
    diff, lo, hi = paired_brier_ci(y_te.astype(float), p_best, p_prior)
    passed, failures = registry.evaluate_gates(sp.gates_key, metrics, baseline)
    if passed and hi >= 0:
        passed, failures = False, [*failures, f"Brier gain over prior not significant (95% CI {lo:.4f}..{hi:.4f})"]
    training = {"rows": {k: len(v) for k, v in parts.items()},
                "positives": {k: int(v.label.sum()) for k, v in parts.items()},
                "window": {k: [str(v.as_of.min()), str(v.as_of.max())] for k, v in parts.items()},
                "validation": valid, "selected": best_name, "brier_vs_prior": [diff, lo, hi],
                "lake_snapshot": lake.snapshot_info(sp.training_table), "data_source": source,
                "evidence": sp.growth(rep) if rep else 0}
    if best_name == "lightgbm":
        training["importance"] = dict(list(gbm_base.importance().items())[:10])

    version = None
    if passed:
        import mlflow

        registry.configure()
        with mlflow.start_run(run_name=f"{sp.name}_{tenant_id}"):
            mlflow.set_tags({"model": sp.name, "tenant_id": tenant_id, "data_source": source,
                             "feature_set": FEATURE_SET_ID, "algorithm": best_name})
            mlflow.log_metrics({f"test_{k}": v for k, v in metrics.items()})
            mlflow.log_metrics({f"prior_{k}": v for k, v in baseline.items() if isinstance(v, float)})
            mlflow.log_dict(training, "training.json")
            info = mlflow.sklearn.log_model(best, name="model",
                                            registered_model_name=registry_name(tenant_id, sp.name),
                                            skops_trusted_types=m1_tenant.TRUSTED_TYPES)  # reviewed allow-list
        version = str(info.registered_model_version)
    stage = "shadow" if passed else "rejected"
    version_key = version or f"rejected-{new_id('mv')}"
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ai.model_versions (tenant_id, model_name, version, registry_name, stage, "
                       "feature_set, algorithm, data_source, metrics, baseline, gates, training, trained_at, "
                       "stage_changed_at) VALUES (:t, :m, :v, :rn, :s, :fs, :alg, :src, CAST(:met AS jsonb), "
                       "CAST(:base AS jsonb), CAST(:g AS jsonb), CAST(:tr AS jsonb), :now, :now)"),
                  {"t": tenant_id, "m": sp.name, "v": version_key, "rn": registry_name(tenant_id, sp.name),
                   "s": stage, "fs": FEATURE_SET_ID, "alg": best_name, "src": source, "met": json.dumps(metrics),
                   "base": json.dumps(baseline), "g": json.dumps({"passed": passed, "failures": failures}),
                   "tr": json.dumps(training, default=str), "now": now})
        record_event(c, tenant_id, version_key, None, stage,
                     "offline gates passed: enters shadow" if passed else "; ".join(failures), training, actor, now,
                     model=sp.name)
    return TrainResult(stage, "trained" if passed else "gates failed", version, metrics, baseline, valid, failures)


def record_event(c: Any, tenant_id: str, version: str, from_stage: str | None, to_stage: str, reason: str,
                 evidence: dict[str, Any], actor: str, now: datetime, model: str = m1_tenant.MODEL_NAME) -> None:
    c.execute(text("INSERT INTO ai.model_events (tenant_id, event_id, model_name, version, from_stage, to_stage, "
                   "reason, evidence, actor, at) VALUES (:t, :e, :m, :v, :f, :to, :r, CAST(:ev AS jsonb), :a, :at)"),
              {"t": tenant_id, "e": new_id("mev"), "m": model, "v": version, "f": from_stage,
               "to": to_stage, "r": reason[:1000], "ev": json.dumps(evidence, default=str), "a": actor, "at": now})
    AuditChain(SqlAuditStore(c), FixedClock(now)).append(tenant_id, actor, "model.stage_changed", {
        "model": model, "version": version, "from": from_stage, "to": to_stage, "reason": reason})



def should_train(engine: Engine, lake: Lake, tenant_id: str,
                 model: str = m1_tenant.MODEL_NAME) -> tuple[bool, str]:
    """Train only for a reason: first model once data is ready, a monitoring trigger since the last training,
    or (after a rejection) enough new labelled evidence to change the answer. Never 'every hour just because'."""
    from nirantar.ml.specs import spec

    sp = spec(model)
    rep = health.latest(engine, tenant_id)
    readiness = (rep or {}).get("readiness", {}).get(sp.readiness_key)
    if rep is None or not readiness or not readiness["ready"]:
        return False, "data not ready"
    with tenant_tx(tenant_id, engine) as c:
        last = c.execute(text("SELECT stage, trained_at, training FROM ai.model_versions WHERE tenant_id=:t AND "
                              "model_name=:m AND feature_set=:fs ORDER BY trained_at DESC LIMIT 1"),
                         {"t": tenant_id, "m": sp.name, "fs": FEATURE_SET_ID}).one_or_none()
        mon = c.execute(text("SELECT report FROM ai.model_monitoring WHERE tenant_id=:t AND model_name=:m "
                             "ORDER BY computed_at DESC LIMIT 1"), {"t": tenant_id, "m": sp.name}
                        ).scalar_one_or_none()
    if last is None:
        return True, "first model for this feature set"
    mon = mon if isinstance(mon, dict) or mon is None else json.loads(mon)
    if mon and mon.get("retrain") and str(mon.get("computed_at", "")) > str(last.trained_at):
        return True, "monitoring: " + "; ".join(mon.get("triggers", []))
    training = last.training if isinstance(last.training, dict) else json.loads(last.training)
    before = training.get("evidence") or sum(training["rows"].values())
    grown = sp.growth(rep) / max(1, before) - 1
    if last.stage == "rejected" and grown >= registry.load_gates("rollout")["retrain_label_growth"]:
        return True, f"last attempt rejected; labelled evidence grew {grown:.0%} since"
    return False, f"latest version is {last.stage}; no trigger"
