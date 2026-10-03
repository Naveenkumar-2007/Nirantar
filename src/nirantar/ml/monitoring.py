"""Model monitoring (P3, ADR-0013): live performance per version, feature drift (Evidently), retrain triggers.

Drift: reference = the champion's (else the latest version's) TRAINING window from gold.training_m1; current =
the feature values actually served in the last 30 days (logged by the router). Evidently's DataDriftPreset
picks the test per column (K-S for numeric, chi²/Z for categorical) and reports the share of drifted columns.
Triggers (ml/gates.yaml → rollout): retrain when drift share ≥ retrain_drift_share, when labelled cycles grew by
retrain_label_growth since training, or when the champion was rolled back. Reports go to ai.model_monitoring.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.data.lake import Lake
from nirantar.db.session import tenant_tx
from nirantar.features.definitions import CATEGORICAL, NUMERIC
from nirantar.ml.models.m1_tenant import MODEL_NAME
from nirantar.ml.rollout import live_performance, rollout_config

MIN_CURRENT_ROWS = 100


def drift_report(reference: pd.DataFrame, current: pd.DataFrame) -> dict[str, Any]:
    from evidently import DataDefinition, Dataset, Report
    from evidently.presets import DataDriftPreset

    # constant columns carry no drift signal and break some tests; drop them from both sides
    cols_num = [c for c in NUMERIC if reference[c].nunique() > 1 or current[c].nunique() > 1]
    cols_cat = [c for c in CATEGORICAL if c in reference and c in current]
    dd = DataDefinition(numerical_columns=cols_num, categorical_columns=cols_cat)
    ref = Dataset.from_pandas(reference[cols_num + cols_cat].astype({c: "float64" for c in cols_num}),
                              data_definition=dd)
    cur = Dataset.from_pandas(current[cols_num + cols_cat].astype({c: "float64" for c in cols_num}),
                              data_definition=dd)
    snap = Report([DataDriftPreset()]).run(cur, ref).dict()
    share, columns = 0.0, {}
    for m in snap["metrics"]:
        name = str(m.get("metric_name") or m.get("metric_id") or "")
        if name.startswith("DriftedColumnsCount"):
            share = float(m["value"]["share"])
        elif name.startswith("ValueDrift"):
            col = name.split("column=", 1)[1].split(",", 1)[0]
            method = name.split("method=", 1)[1].split(",", 1)[0] if "method=" in name else ""
            threshold = float(name.split("threshold=", 1)[1].rstrip(")")) if "threshold=" in name else None
            value = float(m["value"])
            drifted = (value < threshold) if threshold is not None and "p_value" in method else \
                (value > threshold) if threshold is not None else False
            columns[col] = {"method": method, "value": value, "threshold": threshold, "drifted": drifted}
    return {"share_drifted": share, "columns": columns, "reference_rows": len(reference),
            "current_rows": len(current)}


def run_monitoring(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime,
                   model: str = MODEL_NAME) -> dict[str, Any]:
    from nirantar.ml.specs import spec

    training_table = spec(model).training_table
    cfg = rollout_config()
    with tenant_tx(tenant_id, engine) as c:
        perf = live_performance(c, tenant_id, model=model)
        ref_version = c.execute(text(
            "SELECT version, stage, training FROM ai.model_versions WHERE tenant_id=:t AND model_name=:m AND "
            "stage <> 'rejected' ORDER BY (stage='champion') DESC, trained_at DESC LIMIT 1"),
            {"t": tenant_id, "m": model}).one_or_none()
        served = c.execute(text(
            "SELECT output->'features' AS f FROM ai.predictions WHERE tenant_id=:t AND model_name=:m AND "
            "output->>'role'='decision' AND predicted_at >= :since"),
            {"t": tenant_id, "m": model, "since": now - timedelta(days=30)}).all()
        rolled_back: int = c.execute(text(
            "SELECT count(*) FROM ai.model_events WHERE tenant_id=:t AND model_name=:m AND from_stage='champion' "
            "AND to_stage='retired' AND at >= :since"),
            {"t": tenant_id, "m": model, "since": now - timedelta(days=1)}).scalar_one()
    report: dict[str, Any] = {"model": model, "computed_at": now, "performance": perf, "drift": None,
                              "triggers": []}
    training = None
    if ref_version is not None:
        training = ref_version.training if isinstance(ref_version.training, dict) else json.loads(ref_version.training)
    current = pd.DataFrame([r.f if isinstance(r.f, dict) else json.loads(r.f) for r in served])
    ts = lake.read(training_table, tenant_id)
    if ref_version is not None and training is not None and not ts.empty and len(current) >= MIN_CURRENT_ROWS:
        lo, hi = (pd.Timestamp(x) for x in training["window"]["train"])
        reference = ts[(ts.as_of >= lo) & (ts.as_of <= hi)]
        report["drift"] = {**drift_report(reference, current), "reference_version": ref_version.version}
        if report["drift"]["share_drifted"] >= cfg["retrain_drift_share"]:
            report["triggers"].append(f"feature drift {report['drift']['share_drifted']:.0%}")
    elif len(current) < MIN_CURRENT_ROWS:
        report["drift_note"] = f"needs ≥{MIN_CURRENT_ROWS} served predictions in 30 days (have {len(current)})"
    if training is not None and not ts.empty:
        trained_rows = sum(training["rows"].values())
        growth = (len(ts) - trained_rows) / max(1, trained_rows)
        report["label_growth"] = growth
        if growth >= cfg["retrain_label_growth"]:
            report["triggers"].append(f"labelled cycles grew {growth:.0%} since training")
    if rolled_back:
        report["triggers"].append("champion was rolled back")
    report["retrain"] = bool(report["triggers"])
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ai.model_monitoring (tenant_id, report_id, model_name, computed_at, report) "
                       "VALUES (:t, :r, :m, :at, CAST(:rep AS jsonb))"),
                  {"t": tenant_id, "r": new_id("mon"), "m": model, "at": now,
                   "rep": json.dumps(report, default=str)})
    return report
