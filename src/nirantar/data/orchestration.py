"""Dagster orchestration for the data platform (P2, ADR-0012).

Run locally:  uv run dagster dev -m nirantar.data.orchestration -p 3070
- One dynamic partition per tenant. `tenants_sensor` adds a partition when a tenant is onboarded and removes
  it when the tenant is closed. `hourly_refresh` requests one run per tenant partition every hour.
- Assets: bronze_provider_history → bronze_nirantar_changes → silver_tables → gold_charge_outcomes → data_health.
- Asset checks turn contract/quarantine results and label invariants into visible pass/fail.
Every asset calls the same functions as `pipeline.run_tenant` and records its step in `ingest.pipeline_runs`
(run_id = the Dagster run id), so the product UI shows the same history whichever path ran it.
"""

# No `from __future__ import annotations`: Dagster validates the real `context` annotation types, and it parses
# return annotations at runtime (a subscripted MaterializeResult[...] breaks assets that declare check_specs).

import os
from datetime import UTC, datetime, timedelta
from functools import cache, partial
from typing import Any

import dagster as dg
from dagster import AssetExecutionContext, ScheduleEvaluationContext, SensorEvaluationContext
from sqlalchemy import Engine, create_engine, text

from nirantar.data import bronze, gold, health, silver
from nirantar.data.lake import Lake
from nirantar.data.pipeline import finish_run, provider_sources, start_run, timed_step
from nirantar.db.session import get_engine

tenants = dg.DynamicPartitionsDefinition(name="tenants")
GROUP = "nirantar_data"
MAX_QUARANTINE_SHARE = 0.01      # >1% of a table's rows failing contracts is an error, below is a warning
MAX_NEW_TENANTS_PER_TICK = 25    # onboarding bursts are spread over ticks; run concurrency is capped in dagster.yaml
# 24/7 automation is ON in staging/production; in local dev it starts only when NIRANTAR_AUTOMATION=on
# (a dev database accumulates thousands of test tenants).
_AUTO = os.environ.get("NIRANTAR_ENV") in ("staging", "production") or os.environ.get("NIRANTAR_AUTOMATION") == "on"
SENSOR_STATUS = dg.DefaultSensorStatus.RUNNING if _AUTO else dg.DefaultSensorStatus.STOPPED
SCHEDULE_STATUS = dg.DefaultScheduleStatus.RUNNING if _AUTO else dg.DefaultScheduleStatus.STOPPED


@cache
def _engine() -> Engine:
    return get_engine()


@cache
def _lake() -> Lake:
    return Lake.from_env()


def _owner_engine() -> Engine:
    """Platform role, used ONLY to list tenant ids for partitioning (never to read tenant data)."""
    return create_engine(os.environ.get("DATABASE_OWNER_URL",
                                        "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"))


def _ctx(context: AssetExecutionContext) -> tuple[str, str, datetime]:
    return context.partition_key, context.run.run_id, datetime.now(UTC)


@dg.asset(partitions_def=tenants, group_name=GROUP, description="Provider history (backfill), PII redacted.")
def bronze_provider_history(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    start_run(_engine(), tenant, run_id, "dagster")
    rows = 0
    for src in provider_sources(_engine(), tenant):
        res = timed_step(_engine(), tenant, run_id, f"bronze.backfill.{src.provider}",
                         partial(bronze.backfill, _engine(), _lake(), tenant, src, since=now - timedelta(days=365),
                                 until=now, run_id=run_id, now=now))
        rows += res.rows
    return dg.MaterializeResult(metadata={"rows": rows})


@dg.asset(partitions_def=tenants, group_name=GROUP, deps=[bronze_provider_history],
          description="Changed rows from Nirantar tables (transaction-id change capture).")
def bronze_nirantar_changes(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    start_run(_engine(), tenant, run_id, "dagster")
    res = timed_step(_engine(), tenant, run_id, "bronze.changes",
                     lambda: bronze.extract_changes(_engine(), _lake(), tenant, run_id=run_id, now=now))
    return dg.MaterializeResult(metadata={"rows": res.rows})


@dg.asset(partitions_def=tenants, group_name=GROUP, deps=[bronze_nirantar_changes],
          check_specs=[dg.AssetCheckSpec("contracts", asset="silver_tables")],
          description="Typed, deduplicated, contract-checked tables.")
def silver_tables(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    res = timed_step(_engine(), tenant, run_id, "silver",
                     lambda: silver.build_silver(_lake(), tenant, run_id=run_id, now=now))
    share = res.quarantined / max(1, res.rows + res.quarantined)
    severity = dg.AssetCheckSeverity.ERROR if share > MAX_QUARANTINE_SHARE else dg.AssetCheckSeverity.WARN
    return dg.MaterializeResult(
        metadata={"rows": res.rows, "quarantined": res.quarantined},
        check_results=[dg.AssetCheckResult(check_name="contracts", passed=res.quarantined == 0, severity=severity,
                                           metadata={"quarantine_share": share,
                                                     "by_table": {k: v["quarantined"] for k, v in res.tables.items()
                                                                  if v["quarantined"]}})])


@dg.asset(partitions_def=tenants, group_name=GROUP, deps=[silver_tables],
          check_specs=[dg.AssetCheckSpec("label_invariants", asset="gold_charge_outcomes")],
          description="One row per billing cycle: the label table for P3 models.")
def gold_charge_outcomes(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    res = timed_step(_engine(), tenant, run_id, "gold.charge_outcomes",
                     lambda: gold.build_charge_outcomes(_lake(), tenant, now=now))
    from nirantar.data.lifecycle import build_lifecycle

    timed_step(_engine(), tenant, run_id, "gold.subscription_lifecycle",
               lambda: build_lifecycle(_lake(), tenant, now=now))
    problems = gold.invariant_violations(_lake().read(gold.TABLE, tenant))
    return dg.MaterializeResult(
        metadata={"rows": res.rows, "final_labels": res.final_labels, "failures": res.failures},
        check_results=[dg.AssetCheckResult(check_name="label_invariants", passed=not problems,
                                           metadata={"violations": problems})])


@dg.asset(partitions_def=tenants, group_name=GROUP, deps=[gold_charge_outcomes],
          description="Coverage, freshness, quarantine and per-model training readiness.")
def data_health(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    report = timed_step(_engine(), tenant, run_id, "health", lambda: health.compute(_lake(), tenant, now=now))
    health.store(_engine(), tenant, report, run_id, now)
    finish_run(_engine(), tenant, run_id)   # data stage complete; ML assets (if selected) append and re-finish
    ready = [m for m, r in report["readiness"].items() if r["ready"]]
    return dg.MaterializeResult(metadata={"history_days": report["history_days"], "models_ready": ", ".join(ready)
                                          or "none"})


# ------------------------------------------------------------------ P3: features, training, rollout, monitoring
ML_GROUP = "nirantar_ml"


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[data_health],
          description="Point-in-time training set (gold.training_m1) + online store (Redis) materialisation.")
def feature_store(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.features.offline import build_training_set
    from nirantar.features.online import OnlineStore
    from nirantar.ml.tenant_training import _data_source

    ts = timed_step(_engine(), tenant, run_id, "features.offline", lambda: build_training_set(
        _lake(), tenant, now=now, source=_data_source(_engine(), tenant)))
    online = timed_step(_engine(), tenant, run_id, "features.online",
                        lambda: OnlineStore().materialize(_lake(), tenant, now=now))
    return dg.MaterializeResult(metadata={"training_rows": len(ts), **online})


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[feature_store],
          description="Per-tenant M1 training when there is a reason (first model, monitoring trigger, new data).")
def m1_training(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.ml.tenant_training import should_train, train_m1

    go, why = should_train(_engine(), _lake(), tenant)
    if not go:
        return dg.MaterializeResult(metadata={"trained": False, "reason": why})
    res = timed_step(_engine(), tenant, run_id, "ml.train_m1", lambda: train_m1(_engine(), _lake(), tenant, now=now))
    return dg.MaterializeResult(metadata={"trained": True, "why": why, "status": res.status,
                                          "version": res.version or "-", "gate_failures": "; ".join(res.gate_failures)})


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[m1_training],
          description="Shadow → canary → champion on live verified outcomes; automatic rollback.")
def m1_rollout(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.ml.rollout import evaluate_rollout

    moves = timed_step(_engine(), tenant, run_id, "ml.rollout", lambda: evaluate_rollout(_engine(), tenant, now=now))
    return dg.MaterializeResult(metadata={"transitions": len(moves),
                                          "moves": "; ".join(f"{m['version']}:{m['from']}→{m['to']}" for m in moves)})


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[m1_rollout],
          check_specs=[dg.AssetCheckSpec("no_retrain_trigger", asset="m1_monitoring")],
          description="Live performance per version, feature drift (Evidently), retrain triggers.")
def m1_monitoring(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.ml.monitoring import run_monitoring

    rep = timed_step(_engine(), tenant, run_id, "ml.monitoring",
                     lambda: run_monitoring(_engine(), _lake(), tenant, now=now))
    finish_run(_engine(), tenant, run_id)
    drift = rep.get("drift") or {}
    return dg.MaterializeResult(
        metadata={"drift_share": drift.get("share_drifted", -1.0), "retrain": rep["retrain"]},
        check_results=[dg.AssetCheckResult(check_name="no_retrain_trigger", passed=not rep["retrain"],
                                           severity=dg.AssetCheckSeverity.WARN,
                                           metadata={"triggers": "; ".join(rep["triggers"]) or "none"})])


# ------------------------------------------------------------------ P4: churn, value, win-back
@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[feature_store],
          description="sBG tenure fit (CLV + M6 cold-start prior) and churn-type rule (M13 prior).")
def retention_fit(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.retention.fit import fit_retention

    rep = timed_step(_engine(), tenant, run_id, "retention.fit", lambda: fit_retention(_engine(), _lake(), tenant,
                                                                                       now=now))
    return dg.MaterializeResult(metadata={"subscribers": rep["subscribers"], "churned": rep["churned"],
                                          "sbg_fitted": "sbg" in rep, "clv_total_minor": rep.get("clv_total_minor", 0)})


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[retention_fit],
          description="Per-tenant M6 (churn in 60 days) and M13 (churn reason) training when there is a reason.")
def churn_training(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.ml.specs import M6, M13
    from nirantar.ml.tenant_training import should_train, train_model

    meta: dict[str, str] = {}
    for sp in (M6, M13):
        go, why = should_train(_engine(), _lake(), tenant, sp.name)
        if not go:
            meta[sp.name] = f"skipped: {why}"
            continue
        res = timed_step(_engine(), tenant, run_id, f"ml.train_{sp.name}", partial(
            train_model, sp, _engine(), _lake(), tenant, now=now))
        meta[sp.name] = f"{res.status}: {'; '.join(res.gate_failures) or res.reason}"
    return dg.MaterializeResult(metadata=meta)


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[churn_training],
          description="Shadow/canary/champion rollout and monitoring for the churn models.")
def churn_rollout_monitoring(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.ml.monitoring import run_monitoring
    from nirantar.ml.rollout import evaluate_rollout

    meta: dict[str, Any] = {}
    for model in ("m6_churn", "m13_churn_type"):
        moves = timed_step(_engine(), tenant, run_id, f"ml.rollout_{model}",
                           partial(evaluate_rollout, _engine(), tenant, now=now, model=model))
        rep = timed_step(_engine(), tenant, run_id, f"ml.monitoring_{model}",
                         partial(run_monitoring, _engine(), _lake(), tenant, now=now, model=model))
        meta[model] = f"{len(moves)} transitions; retrain={rep['retrain']}"
    return dg.MaterializeResult(metadata=meta)


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[churn_rollout_monitoring],
          description="Label past churn predictions; score active subscribers; store the at-risk list.")
def churn_scoring(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.features.online import OnlineStore
    from nirantar.ml.router import ModelRouter
    from nirantar.retention.launcher import store_at_risk
    from nirantar.retention.scoring import label_predictions, score_active

    labels = timed_step(_engine(), tenant, run_id, "retention.label",
                        lambda: label_predictions(_engine(), _lake(), tenant, now=now))
    rep = timed_step(_engine(), tenant, run_id, "retention.score", lambda: score_active(
        _engine(), _lake(), tenant, now=now, store=OnlineStore(), router=ModelRouter()))
    store_at_risk(_engine(), tenant, rep, now)
    return dg.MaterializeResult(metadata={"labels_written": sum(labels.values()), "scored": rep["scored"],
                                          "unscored": rep["unscored"], "at_risk": len(rep["at_risk"])})


@dg.asset(partitions_def=tenants, group_name=ML_GROUP, deps=[churn_scoring],
          description="Select win-back candidates (consent before randomisation), assign arms, start workflows.")
def winback_campaign(context: AssetExecutionContext) -> dg.MaterializeResult:  # type: ignore[type-arg]  # Dagster parses this at runtime
    tenant, run_id, now = _ctx(context)
    from nirantar.db.session import tenant_tx
    from nirantar.retention.candidates import select_winback
    from nirantar.retention.launcher import start_revivals

    sel = timed_step(_engine(), tenant, run_id, "retention.select",
                     lambda: select_winback(_engine(), _lake(), tenant, now=now))
    with tenant_tx(tenant, _engine()) as c:     # every OPEN case (not just today's): a Temporal outage self-heals
        open_cases = [{"case_id": r.case_id, "window_days": (r.summary or {}).get("window_days", 30)}
                      for r in c.execute(text("SELECT case_id, summary FROM ops.cases WHERE tenant_id=:t AND "
                                              "kind='revival' AND status='open'"), {"t": tenant})]
    try:
        launched = start_revivals(tenant, open_cases)
        finish_run(_engine(), tenant, run_id)
    except Exception as exc:  # Temporal unreachable: cases stay open and are started on the next run
        launched = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        finish_run(_engine(), tenant, run_id, f"winback launch: {launched['error']}")
    return dg.MaterializeResult(metadata={"created": len(sel["created"]), "excluded": str(sel["excluded"]),
                                          **{k: str(v) for k, v in launched.items()}})


tenant_pipeline = dg.define_asset_job("tenant_pipeline",
                                      selection=dg.AssetSelection.groups(GROUP) | dg.AssetSelection.groups(ML_GROUP),
                                      partitions_def=tenants)


@dg.sensor(job=tenant_pipeline, minimum_interval_seconds=60, default_status=SENSOR_STATUS)
def tenants_sensor(context: SensorEvaluationContext) -> dg.SensorResult:
    with _owner_engine().connect() as c:
        active = {r[0] for r in c.execute(text("SELECT tenant_id FROM core.tenants WHERE status='active'"))}
    known = set(context.instance.get_dynamic_partitions("tenants"))
    new, gone = sorted(active - known)[:MAX_NEW_TENANTS_PER_TICK], sorted(known - active)
    return dg.SensorResult(
        run_requests=[dg.RunRequest(partition_key=t, tags={"trigger": "onboarding"}) for t in new],
        dynamic_partitions_requests=[r for r in (tenants.build_add_request(new) if new else None,
                                                  tenants.build_delete_request(gone) if gone else None) if r])


@dg.schedule(job=tenant_pipeline, cron_schedule="0 * * * *", default_status=SCHEDULE_STATUS)
def hourly_refresh(context: ScheduleEvaluationContext) -> list[dg.RunRequest]:
    stamp = context.scheduled_execution_time.strftime("%Y%m%d%H")
    return [dg.RunRequest(partition_key=t, run_key=f"{t}:{stamp}", tags={"trigger": "schedule"})
            for t in context.instance.get_dynamic_partitions("tenants")]


defs = dg.Definitions(
    assets=[bronze_provider_history, bronze_nirantar_changes, silver_tables, gold_charge_outcomes, data_health,
            feature_store, m1_training, m1_rollout, m1_monitoring, retention_fit, churn_training,
            churn_rollout_monitoring, churn_scoring, winback_campaign],
    jobs=[tenant_pipeline], sensors=[tenants_sensor], schedules=[hourly_refresh],
)
