"""One tenant's data pipeline: bronze (backfill + change capture) → silver → gold → health.

Plain functions: Dagster (orchestration.py), the API and tests all call `run_tenant`. Every run is recorded in
`ingest.pipeline_runs` with per-step results; a failed step marks the run failed with the error and stops.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.data import bronze, gold, health, lifecycle, silver
from nirantar.data.lake import Lake
from nirantar.data.sources import BackfillSource, source_for_account
from nirantar.db.session import tenant_tx

BACKFILL_DAYS = 365


def provider_sources(engine: Engine, tenant_id: str) -> list[BackfillSource]:
    """Backfill sources for every connected provider that supports it (credentials via secret refs)."""
    with tenant_tx(tenant_id, engine) as c:
        accounts = c.execute(text("SELECT provider, secret_ref FROM core.provider_accounts WHERE tenant_id=:t "
                                  "AND provider IN ('razorpay')"), {"t": tenant_id}).all()
    return [source_for_account(a.provider, a.secret_ref, tenant_id, engine) for a in accounts]


def _jsonable(v: Any) -> Any:
    if is_dataclass(v) and not isinstance(v, type):
        return asdict(v) if not hasattr(v, "tables") else {"tables": v.tables}
    if isinstance(v, list):
        return [getattr(x, "provider", str(x)) for x in v]       # sources step: provider names only
    return v


# ------------------------------------------------------------------ run records (shared with Dagster assets)
def start_run(engine: Engine, tenant_id: str, run_id: str, trigger: str) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ingest.pipeline_runs (tenant_id, run_id, trigger, status, started_at) "
                       "VALUES (:t, :r, :tr, 'running', :at) ON CONFLICT DO NOTHING"),
                  {"t": tenant_id, "r": run_id, "tr": trigger, "at": datetime.now(UTC)})   # wall clock: ops timing


def record_step(engine: Engine, tenant_id: str, run_id: str, name: str, ms: int, result: Any) -> None:
    entry = json.dumps([{"step": name, "ms": ms, "result": _jsonable(result)}], default=str)
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("UPDATE ingest.pipeline_runs SET steps = steps || CAST(:e AS jsonb) "
                       "WHERE tenant_id=:t AND run_id=:r"), {"e": entry, "t": tenant_id, "r": run_id})


def finish_run(engine: Engine, tenant_id: str, run_id: str, error: str | None = None) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("UPDATE ingest.pipeline_runs SET status=:s, error=:e, finished_at=:at "
                       "WHERE tenant_id=:t AND run_id=:r"),
                  {"s": "failed" if error else "succeeded", "e": error, "at": datetime.now(UTC), "t": tenant_id,
                   "r": run_id})


def timed_step(engine: Engine, tenant_id: str, run_id: str, name: str, fn: Callable[[], Any]) -> Any:
    """Run one step, record it; on error mark the whole run failed (with the reason) and re-raise."""
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:  # recorded, then re-raised: a failed pipeline must be visible
        finish_run(engine, tenant_id, run_id, f"{name}: {type(exc).__name__}: {exc}"[:1000])
        raise
    record_step(engine, tenant_id, run_id, name, int((time.perf_counter() - t0) * 1000), out)
    return out


# ------------------------------------------------------------------ the pipeline
def run_tenant(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime, trigger: str = "manual",
               sources: list[BackfillSource] | None = None, backfill_since: datetime | None = None,
               run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or new_id("run")
    start_run(engine, tenant_id, run_id, trigger)

    def step(name: str, fn: Callable[[], Any]) -> Any:
        return timed_step(engine, tenant_id, run_id, name, fn)

    srcs: list[BackfillSource] = step(
        "sources", lambda: sources if sources is not None else provider_sources(engine, tenant_id))
    since = backfill_since or now - timedelta(days=BACKFILL_DAYS)
    for src in srcs:
        step(f"bronze.backfill.{src.provider}", partial(bronze.backfill, engine, lake, tenant_id, src, since=since,
                                                         until=now, run_id=run_id, now=now))
    step("bronze.changes", lambda: bronze.extract_changes(engine, lake, tenant_id, run_id=run_id, now=now))
    step("silver", lambda: silver.build_silver(lake, tenant_id, run_id=run_id, now=now))
    step("gold.charge_outcomes", lambda: gold.build_charge_outcomes(lake, tenant_id, now=now))
    step("gold.subscription_lifecycle", lambda: lifecycle.build_lifecycle(lake, tenant_id, now=now))
    report = step("health", lambda: health.compute(lake, tenant_id, now=now))
    health.store(engine, tenant_id, report, run_id, now)
    finish_run(engine, tenant_id, run_id)
    with tenant_tx(tenant_id, engine) as c:
        row = c.execute(text("SELECT status, steps FROM ingest.pipeline_runs WHERE tenant_id=:t AND run_id=:r"),
                        {"t": tenant_id, "r": run_id}).one()
    return {"run_id": run_id, "status": row.status, "steps": row.steps, "health": report}
