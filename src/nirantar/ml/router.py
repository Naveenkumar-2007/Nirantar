"""Per-tenant model serving router (P3, ADR-0013).

For each request every live version scores the same features:
  decision  — the model whose score is USED: canary (for its stable hash bucket), else champion, else the prior
  shadow    — logged only; never affects anything
  reference — the prior, always logged, so every version can be compared on the same debits
Each score is stored in ai.predictions with its role, feature-set id and the exact feature values (no PII), which
gives paired live evaluation (rollout.py) and drift monitoring (monitoring.py) without extra plumbing.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.canonical import sha256_hex
from nirantar.core.ids import new_id
from nirantar.features.definitions import CATEGORICAL, FEATURE_SET_ID, NUMERIC
from nirantar.ml.models.m1_tenant import MODEL_NAME, PriorModel

PRIOR_VERSION = "prior-v1"


@dataclass(frozen=True)
class Scored:
    version: str
    role: str
    score: float
    prediction_id: str


@dataclass(frozen=True)
class Decision:
    score: float
    version: str
    prediction_id: str
    served_by: str                      # champion | canary | prior
    scores: list[Scored] = field(default_factory=list)


def bucket(tenant_id: str, subject: str, model: str = MODEL_NAME) -> float:
    """Stable [0, 1) position of a subject for canary routing (same subscription → same model)."""
    h = hashlib.sha256(f"{tenant_id}:{model}:{subject}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def _load(registry_name: str, version: str) -> Any:
    import mlflow

    from nirantar.ml import registry

    registry.configure()
    return mlflow.sklearn.load_model(f"models:/{registry_name}/{version}")


class ModelRouter:
    """Thread-safe cache of loaded model versions; stage changes are read from the DB on every call."""

    def __init__(self, loader: Any = _load) -> None:
        self._loader = loader
        self._cache: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._prior = PriorModel().fit(pd.DataFrame(), None)  # type: ignore[arg-type]

    def _model(self, registry_name: str, version: str) -> Any:
        key = (registry_name, version)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self._loader(registry_name, version)
            return self._cache[key]

    def serving_prior(self, conn: Connection, tenant_id: str, model: str) -> tuple[str, Any] | None:
        """(prior version, transparent prior) used when no trained model decides. M1: history prior (always).
        M6/M13: the tenant's latest retention fit (sBG tenure model / calibrated payment-trouble rule) — None until
        one exists, in which case the model gives no score rather than a made-up one."""
        if model == MODEL_NAME:
            return PRIOR_VERSION, self._prior
        from nirantar.ml.churn import RuleTypePrior, SBGChurnPrior
        from nirantar.ml.specs import spec

        row = conn.execute(text("SELECT fit_id, sbg, type_rule FROM ai.retention_fits WHERE tenant_id=:t "
                                "ORDER BY fitted_at DESC LIMIT 1"), {"t": tenant_id}).one_or_none()
        if row is None:
            return None
        version = f"{spec(model).prior_version}:{row.fit_id}"
        if model == "m6_churn" and row.sbg:
            return version, SBGChurnPrior(float(row.sbg["alpha"]), float(row.sbg["beta"]))
        if model == "m13_churn_type" and row.type_rule:
            return version, RuleTypePrior(float(row.type_rule["p_trouble"]), float(row.type_rule["p_clean"]))
        return None

    def score(self, conn: Connection, tenant_id: str, *, subject_id: str, entity_id: str, features: dict[str, Any],
              now: datetime, context: dict[str, Any] | None = None) -> Decision:
        dec = self.score_model(conn, tenant_id, MODEL_NAME, subject_id=subject_id, entity_id=entity_id,
                               features=features, now=now, context=context)
        assert dec is not None                  # M1 always has its history prior
        return dec

    def score_model(self, conn: Connection, tenant_id: str, model: str, *, subject_id: str, entity_id: str,
                    features: dict[str, Any], now: datetime, context: dict[str, Any] | None = None) -> Decision | None:
        missing = set(NUMERIC + CATEGORICAL) - set(features)
        if missing:
            raise ValueError(f"serving contract violated: missing features {sorted(missing)}")
        live = conn.execute(text("SELECT version, registry_name, stage, canary_share, feature_set FROM ai.model_versions "
                                 "WHERE tenant_id=:t AND model_name=:m AND stage IN ('shadow','canary','champion')"),
                            {"t": tenant_id, "m": model}).all()
        x = pd.DataFrame([features])
        champion = next((v for v in live if v.stage == "champion"), None)
        canary = next((v for v in live if v.stage == "canary"), None)
        decider = canary if canary is not None and bucket(tenant_id, entity_id, model) < canary.canary_share \
            else champion
        prior = self.serving_prior(conn, tenant_id, model)
        if decider is None and prior is None:
            return None
        served_by = "canary" if decider is canary and canary is not None else "champion" if decider else "prior"
        scored: list[Scored] = []
        entries: list[tuple[str, str, float]] = [] if prior is None else [
            (prior[0], "decision" if decider is None else "reference", float(prior[1].predict_proba(x)[0, 1]))]
        for v in live:
            if v.feature_set != FEATURE_SET_ID:
                continue                       # a version trained on another feature set can't read these features
            role = "decision" if decider is not None and v.version == decider.version else "shadow"
            entries.append((v.version, role, float(self._model(v.registry_name, v.version).predict_proba(x)[0, 1])))
        feats_json = {k: (v.item() if hasattr(v, "item") else v) for k, v in features.items()}
        for version, role, p in entries:
            pid = new_id("prd")
            conn.execute(text(
                "INSERT INTO ai.predictions (tenant_id, prediction_id, model_name, model_version, subject_id, "
                "features_hash, score, output, predicted_at) VALUES (:t, :p, :m, :v, :s, :h, :sc, CAST(:o AS jsonb), :n)"),
                {"t": tenant_id, "p": pid, "m": model, "v": version, "s": subject_id,
                 "h": sha256_hex({"fs": FEATURE_SET_ID, "f": feats_json}), "sc": p, "n": now,
                 "o": json.dumps({"role": role, "feature_set": FEATURE_SET_ID, "entity_id": entity_id,
                                  "served_by": served_by, "features": feats_json, **(context or {})}, default=str)})
            scored.append(Scored(version, role, p, pid))
        chosen = next(s for s in scored if s.role == "decision")
        return Decision(chosen.score, chosen.version, chosen.prediction_id, served_by, scored)
