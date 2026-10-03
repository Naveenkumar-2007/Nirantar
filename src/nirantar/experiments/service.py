"""Experiment service: deterministic assignment, exposure/outcome logging, incrementality.

Rules (ADR-0004 d):
- Lending tenants have NO holdout unless they explicitly opt in (tenant setting `holdout_opt_in`).
- A holdout only ever withholds OPTIONAL interventions. Mandatory communications
  (pre-debit notices, agent disclosures, legal/grievance/hardship responses) are never withheld.
- Only verifier-confirmed outcomes count towards incrementality.
- Assignment is a pure function of (experiment, customer), so it is stable across
  retries/replays and never depends on model features (no leakage into models).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.ids import new_id
from nirantar.db.stores import Outbox
from nirantar.settings import service as settings_service
from nirantar.settings.schema import Experiments

HOLDOUT = "holdout"
MANDATORY_ACTIONS = frozenset({
    "predebit_notice", "postdebit_notice", "agent_disclosure", "legal_notice", "grievance_response",
    "hardship_response", "fraud_response", "consent_withdrawal_ack",
})


class ExperimentPolicyError(ValueError):
    pass


def is_withholdable(action_kind: str) -> bool:
    return action_kind not in MANDATORY_ACTIONS


def _bucket(experiment_id: str, customer_id: str) -> int:
    digest = hashlib.sha256(f"{experiment_id}:{customer_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _tenant_profile(conn: Connection, tenant_id: str) -> tuple[bool, bool, int]:
    """(is_lending, holdout_opt_in, configured holdout_bp). Segments are tenant facts (core.tenants); holdout size
    and opt-in are versioned tenant settings (namespace `experiments`, ADR-0011)."""
    raw: Any = conn.execute(text("SELECT settings FROM core.tenants WHERE tenant_id=:t"),
                            {"t": tenant_id}).scalar_one()
    facts: dict[str, Any] = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    cfg = settings_service.model(conn, tenant_id, "experiments", Experiments)
    lending = bool({"lending", "mfi"} & set(facts.get("segments", [])))
    # legacy tenants recorded opt-in on the tenant row; either source counts as an explicit opt-in
    return lending, cfg.holdout_opt_in or bool(facts.get("holdout_opt_in", False)), cfg.holdout_bp


def default_holdout_bp(conn: Connection, tenant_id: str) -> int:
    lending, opt_in, bp = _tenant_profile(conn, tenant_id)
    return bp if (not lending or opt_in) else 0


def create_experiment(conn: Connection, tenant_id: str, name: str, arms_bp: dict[str, int],
                      holdout_bp: int | None = None) -> str:
    lending, opt_in, _ = _tenant_profile(conn, tenant_id)
    if holdout_bp is None:
        holdout_bp = default_holdout_bp(conn, tenant_id)
    if lending and holdout_bp > 0 and not opt_in:
        raise ExperimentPolicyError("lending tenants need explicit holdout opt-in (ADR-0004 d)")
    if HOLDOUT in arms_bp or any(v <= 0 for v in arms_bp.values()):
        raise ExperimentPolicyError("arms must be positive and must not be named 'holdout'")
    if holdout_bp + sum(arms_bp.values()) != 10_000:
        raise ExperimentPolicyError("holdout + arms must sum to 10000 basis points")
    exp_id = new_id("exp")
    conn.execute(
        text("INSERT INTO experiments.experiments (tenant_id, experiment_id, name, holdout_bp, arms) "
             "VALUES (:t, :e, :n, :h, CAST(:a AS jsonb))"),
        {"t": tenant_id, "e": exp_id, "n": name, "h": holdout_bp, "a": json.dumps(arms_bp)},
    )
    return exp_id


def assign(conn: Connection, tenant_id: str, experiment_id: str, customer_id: str, now: datetime) -> str:
    exp = conn.execute(
        text("SELECT holdout_bp, arms, status FROM experiments.experiments WHERE tenant_id=:t AND experiment_id=:e"),
        {"t": tenant_id, "e": experiment_id},
    ).one()
    if exp.status != "running":
        raise ExperimentPolicyError(f"experiment {experiment_id} is {exp.status}")
    arms = exp.arms if isinstance(exp.arms, dict) else json.loads(exp.arms)
    b = _bucket(experiment_id, customer_id)
    if b < exp.holdout_bp:
        arm = HOLDOUT
    else:
        acc = exp.holdout_bp
        arm = next(iter(sorted(arms)))
        for name in sorted(arms):
            acc += arms[name]
            if b < acc:
                arm = name
                break
    conn.execute(
        text("INSERT INTO experiments.assignments (tenant_id, experiment_id, customer_id, arm, assigned_at) "
             "VALUES (:t, :e, :c, :a, :n) ON CONFLICT DO NOTHING"),
        {"t": tenant_id, "e": experiment_id, "c": customer_id, "a": arm, "n": now},
    )
    stored: Any = conn.execute(
        text("SELECT arm FROM experiments.assignments WHERE tenant_id=:t AND experiment_id=:e AND customer_id=:c"),
        {"t": tenant_id, "e": experiment_id, "c": customer_id},
    ).scalar_one()
    return str(stored)


def log_exposure(conn: Connection, tenant_id: str, experiment_id: str, customer_id: str, arm: str,
                 action_ref: str | None, now: datetime) -> str:
    xid = new_id("xpo")
    conn.execute(
        text("INSERT INTO experiments.exposures (tenant_id, exposure_id, experiment_id, customer_id, arm, action_ref,"
             " exposed_at) VALUES (:t, :x, :e, :c, :a, :r, :n)"),
        {"t": tenant_id, "x": xid, "e": experiment_id, "c": customer_id, "a": arm, "r": action_ref, "n": now},
    )
    Outbox(conn).add(make_event(event_type="experiment.exposure", version=1, tenant_id=tenant_id,
                                subject_id=customer_id, payload={"experiment_id": experiment_id, "arm": arm,
                                                                 "action_ref": action_ref},
                                source="experiments", occurred_at=now))
    return xid


def record_outcome(conn: Connection, tenant_id: str, experiment_id: str, customer_id: str, debit_id: str | None,
                   outcome: str, value_minor: int, verified: bool, now: datetime) -> str:
    oid = new_id("out")
    conn.execute(
        text("INSERT INTO experiments.outcomes (tenant_id, outcome_id, experiment_id, customer_id, debit_id, outcome,"
             " value_minor, verified, observed_at) VALUES (:t, :o, :e, :c, :d, :oc, :v, :vf, :n)"),
        {"t": tenant_id, "o": oid, "e": experiment_id, "c": customer_id, "d": debit_id, "oc": outcome,
         "v": value_minor, "vf": verified, "n": now},
    )
    return oid


@dataclass(frozen=True)
class ArmStats:
    arm: str
    n: int
    successes: int
    value_minor: int

    @property
    def rate(self) -> float:
        return self.successes / self.n if self.n else 0.0


def analyze(conn: Connection, tenant_id: str, experiment_id: str, min_per_arm: int = 30,
            success_outcome: str = "recovered") -> dict[str, Any]:
    """Per-arm recovery rate and ₹, incremental vs holdout with 95% CIs (verified outcomes only).

    Units are assigned customers; a customer counts as success if any verified 'recovered' outcome exists.
    """
    rows = conn.execute(
        text(
            "SELECT a.arm, a.customer_id, "
            "coalesce(bool_or(o.verified AND o.outcome = :so), false) AS success, "
            "coalesce(sum(o.value_minor) FILTER (WHERE o.verified AND o.outcome = :so), 0) AS value "
            "FROM experiments.assignments a LEFT JOIN experiments.outcomes o "
            "ON o.tenant_id=a.tenant_id AND o.experiment_id=a.experiment_id AND o.customer_id=a.customer_id "
            "WHERE a.tenant_id=:t AND a.experiment_id=:e GROUP BY a.arm, a.customer_id"
        ),
        {"t": tenant_id, "e": experiment_id, "so": success_outcome},
    ).all()
    per: dict[str, list[tuple[bool, int]]] = {}
    for r in rows:
        per.setdefault(r.arm, []).append((bool(r.success), int(r.value)))
    stats = {arm: ArmStats(arm, len(v), sum(s for s, _ in v), sum(x for _, x in v)) for arm, v in per.items()}
    result: dict[str, Any] = {"experiment_id": experiment_id, "arms": {
        a: {"n": s.n, "recovery_rate": s.rate, "value_minor": s.value_minor} for a, s in stats.items()}}
    control = stats.get(HOLDOUT)
    if control is None or control.n < min_per_arm:
        result["incremental"] = None
        result["note"] = "insufficient holdout sample for incrementality"
        return result
    inc = {}
    for arm, s in stats.items():
        if arm == HOLDOUT or s.n < min_per_arm:
            continue
        diff = s.rate - control.rate
        se = math.sqrt(s.rate * (1 - s.rate) / s.n + control.rate * (1 - control.rate) / control.n)
        vals_t = [x for _, x in per[arm]]
        vals_c = [x for _, x in per[HOLDOUT]]
        mt, mc = sum(vals_t) / len(vals_t), sum(vals_c) / len(vals_c)
        vt = sum((x - mt) ** 2 for x in vals_t) / max(1, len(vals_t) - 1)
        vc = sum((x - mc) ** 2 for x in vals_c) / max(1, len(vals_c) - 1)
        se_v = math.sqrt(vt / len(vals_t) + vc / len(vals_c))
        inc[arm] = {
            "incremental_recovery_rate": diff, "ci95": [diff - 1.96 * se, diff + 1.96 * se],
            "incremental_value_per_customer_minor": mt - mc,
            "value_ci95_minor": [mt - mc - 1.96 * se_v, mt - mc + 1.96 * se_v],
            "incremental_value_total_minor": (mt - mc) * s.n,
            "significant": diff - 1.96 * se > 0,
        }
    result["incremental"] = inc
    return result


RECOVERY_EXPERIMENT = "recovery"


def ensure_recovery_experiment(conn: Connection, tenant_id: str) -> str:
    """The tenant's running recovery experiment, created on first use with the tenant's holdout settings.

    Every DebitCycleWorkflow is assigned to it, so recovery uplift is always measured against a holdout. An
    advisory lock makes concurrent first debits agree on one experiment.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"recovery-exp:{tenant_id}"})
    exp: str | None = conn.execute(
        text("SELECT experiment_id FROM experiments.experiments WHERE tenant_id=:t AND name=:n AND status='running' "
             "ORDER BY created_at LIMIT 1"), {"t": tenant_id, "n": RECOVERY_EXPERIMENT}).scalar_one_or_none()
    if exp:
        return exp
    holdout = default_holdout_bp(conn, tenant_id)
    return create_experiment(conn, tenant_id, RECOVERY_EXPERIMENT, {"treatment": 10_000 - holdout}, holdout)
