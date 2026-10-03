"""Data health: what a tenant has, how fresh and clean it is, and which models it can train (P2 → P3 gate).
Stored in `ai.data_health` (Postgres) so the API/UI never need lake access."""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from sqlalchemy import Engine, text

from nirantar.data import gold
from nirantar.data.bronze import CHANGES_TABLE, PROVIDER_TABLE
from nirantar.data.lake import Lake
from nirantar.db.session import tenant_tx

LAYER_TABLES = {
    "bronze": [PROVIDER_TABLE, CHANGES_TABLE],
    "silver": ["silver.provider_payments", "silver.provider_subscriptions", "silver.provider_invoices",
               "silver.debits", "silver.payments", "silver.customers", "silver.subscriptions", "silver.contacts",
               "silver.outcomes", "silver.labels", "silver.predictions"],
    "gold": ["gold.charge_outcomes", "gold.subscription_lifecycle"],
}


@cache
def readiness_rules() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((Path(__file__).parent / "readiness.yaml").read_text(encoding="utf-8"))
    return data


def _days(series: pd.Series) -> int:
    s = series.dropna()
    return 0 if s.empty else int((s.max() - s.min()).total_seconds() // 86400)


MIN_RATE_COHORT = 30     # below this many matured failures a recovery rate is noise: report the count, not a rate


def compute(lake: Lake, tenant_id: str, *, now: datetime, min_rate_cohort: int = MIN_RATE_COHORT) -> dict[str, Any]:
    counts = {layer: {t: len(lake.read(t, tenant_id, ("tenant_id",))) for t in tables}
              for layer, tables in LAYER_TABLES.items()}
    q = lake.read("silver.quarantine", tenant_id)
    quarantine = {"rows": len(q), "by_table": q.table_name.value_counts().to_dict() if not q.empty else {},
                  "top_reasons": q.reason.value_counts().head(5).to_dict() if not q.empty else {}}
    co = lake.read("gold.charge_outcomes", tenant_id)
    # Two labels with different availability (avoid censoring bias):
    #  - "first attempt failed" is known as soon as the first attempt happens → use every attempted cycle;
    #  - "recovered" is only known once the cycle is final (paid, or past the recovery horizon).
    attempted = co[co.first_attempt_failed.notna()] if not co.empty else co
    failed = attempted[attempted.first_attempt_failed.astype(bool)] if len(attempted) else attempted
    failed_final = failed[failed.label_final] if len(failed) else failed
    # Recovery RATE only on a matured cohort (first attempt older than the horizon): recovered cycles settle as
    # soon as they are paid but unrecovered ones only after the horizon, so "settled" failures over-count recoveries.
    matured = failed[failed.first_attempt_at <= pd.Timestamp(now) - gold.RECOVERY_HORIZON] if len(failed) else failed
    labels = {
        "cycles": len(co), "attempted_cycles": len(attempted),
        "final_cycles": int(co.label_final.sum()) if len(co) else 0,
        "first_attempt_failures": len(failed),
        "failure_rate": round(len(failed) / len(attempted), 4) if len(attempted) else None,
        "recovered_cycles": int(failed_final.recovered.sum()) if len(failed_final) else 0,
        "recovery_rate": round(float(matured.recovered.mean()), 4) if len(matured) >= min_rate_cohort else None,
        "recovery_cohort": len(matured),
        "failures_awaiting_outcome": len(failed) - len(failed_final),
        "by_source": co.source.value_counts().to_dict() if len(co) else {},
        "first_cycle_at": None if co.empty else co.first_attempt_at.min(),
        "last_cycle_at": None if co.empty else co.first_attempt_at.max(),
    }
    history_days = _days(co.first_attempt_at) if not co.empty else 0
    # churn evidence comes from the lifecycle table (provider end states + inferred lapses), not raw statuses
    lc = lake.read("gold.subscription_lifecycle", tenant_id)
    subs_total = len(lc)
    ended = int((lc.status == "churned").sum()) if len(lc) else 0
    have = {"attempted_cycles": labels["attempted_cycles"], "final_cycles": labels["final_cycles"],
            "first_attempt_failures": labels["first_attempt_failures"],
            "recovered_cycles": labels["recovered_cycles"], "history_days": history_days,
            "subscriptions": subs_total, "churned_subscriptions": ended,
            "involuntary_churn": int((lc.churn_type == "involuntary").sum()) if len(lc) else 0,
            "voluntary_churn": int((lc.churn_type == "voluntary").sum()) if len(lc) else 0}
    readiness = {}
    for model, rule in readiness_rules()["models"].items():
        gaps = {k: {"need": v, "have": have.get(k, 0)} for k, v in rule["requires"].items() if have.get(k, 0) < v}
        readiness[model] = {"title": rule["title"], "ready": not gaps, "gaps": gaps}
    return {"computed_at": now, "layers": counts, "quarantine": quarantine, "labels": labels,
            "history_days": history_days, "subscriptions": {
                "total": subs_total, "churned": ended,
                "involuntary": int((lc.churn_type == "involuntary").sum()) if len(lc) else 0,
                "voluntary": int((lc.churn_type == "voluntary").sum()) if len(lc) else 0,
                "completed": int((lc.status == "completed").sum()) if len(lc) else 0},
            "readiness": readiness, "rules_version": readiness_rules()["version"]}


def store(engine: Engine, tenant_id: str, report: dict[str, Any], run_id: str, now: datetime) -> None:
    with tenant_tx(tenant_id, engine) as c:
        sources = [dict(r._mapping) for r in c.execute(text(
            "SELECT provider, mode, created_at FROM core.provider_accounts WHERE tenant_id=:t"), {"t": tenant_id})]
        checkpoints = [dict(r._mapping) for r in c.execute(text(
            "SELECT provider, entity, max(window_end) AS settled_until, sum(rows) AS rows FROM "
            "ingest.backfill_checkpoints WHERE tenant_id=:t GROUP BY 1, 2"), {"t": tenant_id})]
        full = {**report, "sources": sources, "backfill": checkpoints}
        c.execute(text("INSERT INTO ai.data_health (tenant_id, computed_at, run_id, report) VALUES "
                       "(:t, :at, :r, CAST(:rep AS jsonb))"),
                  {"t": tenant_id, "at": now, "r": run_id, "rep": json.dumps(full, default=str)})


def latest(engine: Engine, tenant_id: str) -> dict[str, Any] | None:
    with tenant_tx(tenant_id, engine) as c:
        r = c.execute(text("SELECT report FROM ai.data_health WHERE tenant_id=:t "
                           "ORDER BY computed_at DESC, run_id DESC LIMIT 1"),
                      {"t": tenant_id}).scalar_one_or_none()
    return None if r is None else (r if isinstance(r, dict) else json.loads(r))
