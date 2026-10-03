"""Online rollout on live, verified outcomes (P3, ADR-0013): shadow → canary → champion, and automatic rollback.

All comparisons are PAIRED: the same debits scored by the challenger and by the model that actually decided
(the router scores every live version on every request), joined to the verifier's `debit_failed` label.
Rules (ml/gates.yaml → rollout):
  shadow  → canary    ≥ min_shadow_pairs and 95% upper bound of Brier(challenger − incumbent) ≤ non_inferiority
  shadow  → retired   the challenger is significantly worse (95% lower bound > 0)
  canary  → champion  ≥ min_canary_pairs since the canary started, still non-inferior, live ECE ≤ max_live_ece;
                      the previous champion is retired
  champion→ retired   (rollback) live ECE on the last 500 labels > max_live_ece, or significantly worse than
                      the prior on the same debits — decisions fall back to the prior (never to an untested model)
Every transition is an ai.model_events row + a hash-chained audit record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.db.session import tenant_tx
from nirantar.ml.metrics import expected_calibration_error
from nirantar.ml.models.m1_tenant import MODEL_NAME
from nirantar.ml.registry import load_gates
from nirantar.ml.tenant_training import paired_brier_ci, record_event


def rollout_config() -> dict[str, Any]:
    return load_gates("rollout")


def labelled_scores(conn: Connection, tenant_id: str, since: datetime | None = None,
                    model: str = MODEL_NAME) -> pd.DataFrame:
    """One row per (subject, version): score, role, and the model's own verified label (column `failed` = y)."""
    from nirantar.ml.specs import spec

    sp = spec(model)
    rows = conn.execute(text(
        "SELECT p.subject_id, p.model_version AS version, p.output->>'role' AS role, p.score, p.predicted_at, "
        "(l.value->>'value')::boolean AS failed FROM ai.predictions p JOIN ai.labels l ON l.tenant_id=p.tenant_id "
        "AND l.subject_id=p.subject_id AND l.label_name=:ln AND l.source=:ls "
        "WHERE p.tenant_id=:t AND p.model_name=:m AND p.output ? 'role' AND (CAST(:s AS timestamptz) IS NULL "
        "OR p.predicted_at >= CAST(:s AS timestamptz))"),
        {"t": tenant_id, "m": model, "s": since, "ln": sp.label_name, "ls": sp.label_source}).all()
    return pd.DataFrame([dict(r._mapping) for r in rows],
                        columns=["subject_id", "version", "role", "score", "predicted_at", "failed"])


def paired(df: pd.DataFrame, version: str, against: str = "decision") -> pd.DataFrame:
    """Subjects scored by `version` and by the comparator: the deciding model, or a version PREFIX (e.g. the prior,
    whose version carries its fit id for M6/M13)."""
    mine = df[df.version == version].drop_duplicates("subject_id", keep="last")
    other = (df[(df.role == "decision") & (df.version != version)] if against == "decision"
             else df[df.version.astype(str).str.startswith(against)]).drop_duplicates("subject_id", keep="last")
    m = mine.merge(other, on="subject_id", suffixes=("", "_inc"))
    return m[["subject_id", "score", "score_inc", "failed"]]


def compare(pairs: pd.DataFrame) -> dict[str, Any]:
    if pairs.empty:
        return {"n": 0}
    y = pairs.failed.astype(float).to_numpy()
    diff, lo, hi = paired_brier_ci(y, pairs.score.to_numpy(float), pairs.score_inc.to_numpy(float))
    return {"n": len(pairs), "brier_diff": diff, "ci95": [lo, hi],
            "ece": expected_calibration_error(y, pairs.score.to_numpy(float))}


def _set_stage(c: Connection, tenant_id: str, version: str, frm: str, to: str, reason: str,
               evidence: dict[str, Any], actor: str, now: datetime, canary_share: float | None = None,
               model: str = MODEL_NAME) -> None:
    c.execute(text("UPDATE ai.model_versions SET stage=:to, stage_changed_at=:now, "
                   "canary_share=coalesce(:cs, canary_share) WHERE tenant_id=:t AND model_name=:m AND version=:v "
                   "AND stage=:frm"), {"to": to, "now": now, "cs": canary_share, "t": tenant_id, "m": model,
                                       "v": version, "frm": frm})
    record_event(c, tenant_id, version, frm, to, reason, evidence, actor, now, model=model)


def evaluate_rollout(engine: Engine, tenant_id: str, *, now: datetime, actor: str = "system:rollout",
                     model: str = MODEL_NAME) -> list[dict[str, Any]]:
    from nirantar.ml.specs import spec

    prior_version = spec(model).prior_version
    cfg = rollout_config()
    moves: list[dict[str, Any]] = []
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"rollout:{tenant_id}:{model}"})
        versions = c.execute(text("SELECT version, stage, stage_changed_at FROM ai.model_versions WHERE tenant_id=:t "
                                  "AND model_name=:m AND stage IN ('shadow','canary','champion')"),
                             {"t": tenant_id, "m": model}).all()
        df = labelled_scores(c, tenant_id, model=model)

        def move(v: Any, to: str, reason: str, ev: dict[str, Any], share: float | None = None) -> None:
            _set_stage(c, tenant_id, v.version, v.stage, to, reason, ev, actor, now, share, model=model)
            moves.append({"version": v.version, "from": v.stage, "to": to, "reason": reason, "evidence": ev})

        champion = next((v for v in versions if v.stage == "champion"), None)
        canary = next((v for v in versions if v.stage == "canary"), None)
        # 1. champion health (rollback to the prior)
        if champion is not None:
            recent = df[(df.version == champion.version) & (df.role == "decision")].sort_values("predicted_at").tail(500)
            vs_prior = compare(paired(df, champion.version, against=prior_version))
            ece = expected_calibration_error(recent.failed.astype(float).to_numpy(), recent.score.to_numpy(float)) \
                if len(recent) >= 200 else None
            worse = vs_prior.get("n", 0) >= cfg["min_shadow_pairs"] and vs_prior["ci95"][0] > 0
            if (ece is not None and ece > cfg["max_live_ece"]) or worse:
                move(champion, "retired", f"rollback: live ECE {ece} / worse than prior={worse}",
                     {"live_ece": ece, "vs_prior": vs_prior})
                champion = None
        # 2. canary → champion
        if canary is not None:
            ev = compare(paired(df[df.predicted_at >= canary.stage_changed_at], canary.version))
            if ev["n"] >= cfg["min_canary_pairs"]:
                if ev["ci95"][1] <= cfg["non_inferiority_brier"] and ev["ece"] <= cfg["max_live_ece"]:
                    if champion is not None:
                        move(champion, "retired", f"replaced by {canary.version}", ev)
                    move(canary, "champion", "canary non-inferior on live outcomes", ev)
                elif ev["ci95"][0] > 0:
                    move(canary, "retired", "canary worse than incumbent on live outcomes", ev)
        # 3. shadow → canary / retired
        canary_busy = any(m["to"] == "canary" for m in moves) or (
            canary is not None and not any(m["version"] == canary.version for m in moves))
        for v in sorted((v for v in versions if v.stage == "shadow"), key=lambda v: v.stage_changed_at):
            ev = compare(paired(df, v.version))
            if ev["n"] < cfg["min_shadow_pairs"]:
                continue
            if ev["ci95"][0] > 0:
                move(v, "retired", "shadow significantly worse than the deciding model", ev)
            elif ev["ci95"][1] <= cfg["non_inferiority_brier"] and not canary_busy:
                move(v, "canary", "shadow non-inferior on live outcomes", ev, share=float(cfg["canary_share"]))
                canary_busy = True
    return moves


def manual_rollback(engine: Engine, tenant_id: str, version: str, *, reason: str, actor: str,
                    now: datetime, model: str = MODEL_NAME) -> dict[str, Any]:
    """Human override: retire a live version (champion/canary/shadow). Decisions fall back to the prior/champion."""
    if not reason.strip():
        raise ValueError("a reason is required")
    with tenant_tx(tenant_id, engine) as c:
        row = c.execute(text("SELECT stage FROM ai.model_versions WHERE tenant_id=:t AND model_name=:m AND version=:v"),
                        {"t": tenant_id, "m": model, "v": version}).one_or_none()
        if row is None or row.stage not in ("shadow", "canary", "champion"):
            raise ValueError("version is not live")
        _set_stage(c, tenant_id, version, row.stage, "retired", f"manual: {reason.strip()}", {}, actor, now,
                   model=model)
    return {"version": version, "from": row.stage, "to": "retired"}


def live_performance(conn: Connection, tenant_id: str, model: str = MODEL_NAME) -> dict[str, Any]:
    df = labelled_scores(conn, tenant_id, model=model)
    out: dict[str, Any] = {}
    for version, g in df.groupby("version"):
        y, p = g.failed.astype(float).to_numpy(), g.score.to_numpy(float)
        rep: dict[str, Any] = {"n": len(g), "brier": float(np.mean((p - y) ** 2)), "base_rate": float(y.mean()),
                               "roles": g.role.value_counts().to_dict()}
        if len(g) >= 100:
            rep["ece"] = expected_calibration_error(y, p)
        if len(set(y)) == 2 and len(g) >= 100:
            from sklearn.metrics import roc_auc_score

            rep["auc"] = float(roc_auc_score(y, p))
        out[str(version)] = rep
    return out
