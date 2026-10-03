"""Learn decision parameters from a tenant's own VERIFIED outcomes (P1, ADR-0011).

1. Contact effects (used by the Contact Arbiter) — per failure category:
   The holdout is randomised at customer level and only withholds optional contact, so within a category
       ITT  = P(recovered | assigned treatment) − P(recovered | holdout)            (unbiased)
       CACE = ITT / contact_rate                                                    (Wald / IV estimator)
   is the effect of actually being contacted (exclusion restriction: holdout customers get no optional contact).
   When both channels were used in a category the CACE is split in proportion to the prior ratio between the
   channels (the data cannot separate them without channel-level randomisation — stated in the evidence).
   The estimate is shrunk towards the prior: (k·prior + n·estimate) / (k + n), n = min(contacted, holdout),
   k = settings.effects.prior_strength. Categories below `min_per_group` keep the prior.

2. M1 risk threshold (used by the Debit Strategist) — cost-sensitive:
       value(t) = Σ_{score ≥ t} ( failed · amount · prevention_share − flag_cost )
   over debits with an M1 prediction and a verifier label; pick argmax_t. In-sample on past outcomes; the
   evidence reports the value at the fixed fallback for comparison. Below `min_labels`/`min_positives` → fallback.

Every refresh stores a new version with its evidence (append-only), so any decision can be traced to the numbers
that were in force at the time.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.agents.failure_triage import TriageIn, triage
from nirantar.audit.chain import AuditChain
from nirantar.core.clock import FixedClock
from nirantar.db.stores import SqlAuditStore
from nirantar.settings import service
from nirantar.settings.schema import ARMS, CATEGORIES, Effects, Strategy


@dataclass(frozen=True)
class Learned:
    name: str
    version: int
    value: dict[str, Any]
    evidence: dict[str, Any]
    created_at: datetime


# ------------------------------------------------------------------ storage
def latest(conn: Connection, tenant_id: str, name: str) -> Learned | None:
    r = conn.execute(text("SELECT version, value, evidence, created_at FROM config.learned_params WHERE tenant_id=:t "
                          "AND name=:n ORDER BY version DESC LIMIT 1"), {"t": tenant_id, "n": name}).one_or_none()
    if r is None:
        return None
    return Learned(name, r.version, _obj(r.value), _obj(r.evidence), r.created_at)


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else dict(json.loads(v))


def _store(conn: Connection, tenant_id: str, name: str, value: dict[str, Any], evidence: dict[str, Any],
           now: datetime) -> Learned:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"learned:{tenant_id}:{name}"})
    prev = latest(conn, tenant_id, name)
    version = (prev.version if prev else 0) + 1
    conn.execute(text("INSERT INTO config.learned_params (tenant_id, name, version, value, evidence, created_at) "
                      "VALUES (:t, :n, :v, CAST(:val AS jsonb), CAST(:ev AS jsonb), :at)"),
                 {"t": tenant_id, "n": name, "v": version, "val": json.dumps(value), "ev": json.dumps(evidence),
                  "at": now})
    AuditChain(SqlAuditStore(conn), FixedClock(now)).append(tenant_id, "system:learning", "learned.updated",
                                                            {"name": name, "version": version, "value": value})
    return Learned(name, version, value, evidence, now)


# ------------------------------------------------------------------ 1. contact effects
def _category(code: str | None, reason: str | None) -> str:
    return triage(TriageIn(error_code=code, error_reason=reason), llm=None).category


def effect_rows(conn: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """One row per verified failed-debit outcome: category, assigned arm, channel actually used (if any)."""
    rows = conn.execute(text("""
        SELECT o.outcome, o.debit_id,
               (SELECT a.arm FROM experiments.assignments a WHERE a.tenant_id=o.tenant_id
                  AND a.experiment_id=o.experiment_id AND a.customer_id=o.customer_id) AS assigned,
               (SELECT e.arm FROM experiments.exposures e WHERE e.tenant_id=o.tenant_id
                  AND e.experiment_id=o.experiment_id AND e.customer_id=o.customer_id AND e.exposed_at <= o.observed_at
                  AND e.arm <> 'deferred' ORDER BY e.exposed_at DESC LIMIT 1) AS exposed,
               (SELECT p.error_code FROM billing.payments p WHERE p.tenant_id=o.tenant_id AND p.debit_id=o.debit_id
                  AND p.status='failed' ORDER BY p.created_at LIMIT 1) AS error_code,
               (SELECT p.error_reason FROM billing.payments p WHERE p.tenant_id=o.tenant_id AND p.debit_id=o.debit_id
                  AND p.status='failed' ORDER BY p.created_at LIMIT 1) AS error_reason
        FROM experiments.outcomes o
        WHERE o.tenant_id=:t AND o.verified AND o.outcome IN ('recovered','unrecovered')"""), {"t": tenant_id}).all()
    return [{"recovered": r.outcome == "recovered", "assigned": r.assigned, "exposed": r.exposed,
             "category": _category(r.error_code, r.error_reason)} for r in rows if r.assigned is not None]


def estimate_effects(rows: list[dict[str, Any]], cfg: Effects) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    effects = {c: dict(cfg.prior[c]) for c in CATEGORIES}
    evidence: dict[str, Any] = {"method": "ITT/contact-rate (Wald) with shrinkage to prior",
                                "prior_strength": cfg.prior_strength, "min_per_group": cfg.min_per_group,
                                "outcomes_used": len(rows), "categories": {}}
    for cat in CATEGORIES:
        grp = [r for r in rows if r["category"] == cat]
        treated = [r for r in grp if r["assigned"] != "holdout"]
        hold = [r for r in grp if r["assigned"] == "holdout"]
        used = {arm: sum(1 for r in treated if r["exposed"] == arm) for arm in ARMS}
        contacted = sum(used.values())
        ev: dict[str, Any] = {"treated": len(treated), "holdout": len(hold), "contacted": used}
        if len(treated) < cfg.min_per_group or len(hold) < cfg.min_per_group or contacted == 0:
            ev["source"] = "prior"
            ev["why"] = "not enough treated/holdout outcomes, or no contact made in this category"
            evidence["categories"][cat] = ev
            continue
        p_t = sum(r["recovered"] for r in treated) / len(treated)
        p_h = sum(r["recovered"] for r in hold) / len(hold)
        itt = p_t - p_h
        se_itt = math.sqrt(p_t * (1 - p_t) / len(treated) + p_h * (1 - p_h) / len(hold))
        rate = contacted / len(treated)
        cace, se_cace = itt / rate, se_itt / rate
        share = {arm: used[arm] / contacted for arm in ARMS}
        mix = sum(share[a] * cfg.prior[cat][a] for a in ARMS)
        n = min(contacted, len(hold))
        k = cfg.prior_strength
        for arm in ARMS:
            if used[arm] == 0:
                continue                                   # no data for this channel here → keep its prior
            est = cace * (cfg.prior[cat][arm] / mix if mix > 0 else 1.0)
            effects[cat][arm] = round(min(1.0, max(0.0, (k * cfg.prior[cat][arm] + n * est) / (k + n))), 4)
        ev.update({"source": "learned", "recovery_treated": round(p_t, 4), "recovery_holdout": round(p_h, 4),
                   "itt": round(itt, 4), "itt_ci95": [round(itt - 1.96 * se_itt, 4), round(itt + 1.96 * se_itt, 4)],
                   "contact_rate": round(rate, 4), "cace": round(cace, 4),
                   "cace_ci95": [round(cace - 1.96 * se_cace, 4), round(cace + 1.96 * se_cace, 4)],
                   "effective_n": n, "posterior": effects[cat],
                   "channel_split": "prior ratio" if all(used[a] for a in ARMS) else "single channel"})
        evidence["categories"][cat] = ev
    return effects, evidence


# ------------------------------------------------------------------ 2. risk threshold
def threshold_rows(conn: Connection, tenant_id: str) -> list[tuple[float, bool, int]]:
    rows = conn.execute(text("""
        SELECT p.score, (l.value->>'value')::boolean AS failed, d.amount_minor
        FROM ai.predictions p
        JOIN ai.labels l ON l.tenant_id=p.tenant_id AND l.prediction_id=p.prediction_id AND l.label_name='debit_failed'
        JOIN billing.debits d ON d.tenant_id=p.tenant_id AND d.debit_id=p.subject_id
        WHERE p.tenant_id=:t AND p.model_name='m1_debit_failure' AND p.score IS NOT NULL"""), {"t": tenant_id}).all()
    return [(float(r.score), bool(r.failed), int(r.amount_minor)) for r in rows]


def estimate_threshold(rows: list[tuple[float, bool, int]], cfg: Strategy) -> tuple[float | None, dict[str, Any]]:
    rt = cfg.risk_threshold
    pos = sum(1 for _, y, _ in rows if y)
    ev: dict[str, Any] = {"method": "cost-sensitive argmax on verified outcomes (in-sample)", "labels": len(rows),
                          "positives": pos, "flag_cost_minor": rt.flag_cost_minor,
                          "prevention_share": rt.prevention_share, "fallback": rt.fixed}
    if len(rows) < rt.min_labels or pos < rt.min_positives:
        ev["why"] = f"needs ≥{rt.min_labels} labels and ≥{rt.min_positives} failures"
        return None, ev

    def value(t: float) -> float:
        return sum((amt * rt.prevention_share if y else 0.0) - rt.flag_cost_minor for s, y, amt in rows if s >= t)

    grid = [round(0.01 * i, 2) for i in range(1, 100)]
    best = max(grid, key=lambda t: (value(t), -t))
    flagged = [(s, y) for s, y, _ in rows if s >= best]
    tp = sum(1 for _, y in flagged if y)
    ev.update({"threshold": best, "value_minor_at_threshold": round(value(best)),
               "value_minor_at_fallback": round(value(rt.fixed)), "flagged": len(flagged),
               "precision": round(tp / len(flagged), 4) if flagged else None, "recall": round(tp / pos, 4)})
    return best, ev


# ------------------------------------------------------------------ refresh + effective values
def refresh(conn: Connection, tenant_id: str, now: datetime) -> dict[str, Any]:
    eff_cfg = service.model(conn, tenant_id, "effects", Effects)
    effects, eff_ev = estimate_effects(effect_rows(conn, tenant_id), eff_cfg)
    e = _store(conn, tenant_id, "effects", {"effects": effects}, eff_ev, now)
    strat = service.model(conn, tenant_id, "strategy", Strategy)
    thr, thr_ev = estimate_threshold(threshold_rows(conn, tenant_id), strat)
    t = _store(conn, tenant_id, "risk_threshold", {"threshold": thr}, thr_ev, now)
    return {"effects": {"version": e.version, "value": e.value, "evidence": e.evidence},
            "risk_threshold": {"version": t.version, "value": t.value, "evidence": t.evidence}}


def effective_effects(conn: Connection, tenant_id: str) -> tuple[dict[str, dict[str, float]], str]:
    cfg = service.model(conn, tenant_id, "effects", Effects)
    if cfg.mode == "learned":
        learned = latest(conn, tenant_id, "effects")
        if learned is not None:
            cats = learned.evidence.get("categories", {})
            n = sum(1 for ev in cats.values() if ev.get("source") == "learned")
            if n == 0:   # nothing had enough evidence: say so instead of claiming "learned"
                return {c: dict(v) for c, v in cfg.prior.items()}, "prior:insufficient_evidence"
            return learned.value["effects"], f"learned:v{learned.version} ({n}/{len(cats)} categories)"
    return {c: dict(v) for c, v in cfg.prior.items()}, "prior"


def effective_threshold(conn: Connection, tenant_id: str) -> tuple[float, str]:
    rt = service.model(conn, tenant_id, "strategy", Strategy).risk_threshold
    if rt.mode == "learned":
        learned = latest(conn, tenant_id, "risk_threshold")
        if learned is not None and learned.value.get("threshold") is not None:
            return float(learned.value["threshold"]), f"learned:v{learned.version}"
        return rt.fixed, "fallback:insufficient_evidence"
    return rt.fixed, "fixed"
