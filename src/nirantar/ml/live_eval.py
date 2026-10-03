"""Live evaluation: join logged predictions to verified labels and update the evaluation record.

This is the "model pipeline consumes outcome → evaluation updates" link (BB-§53). The same join
produces training rows for per-tenant retraining.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from nirantar.db.session import tenant_tx

RESULTS = Path(__file__).resolve().parents[3] / "evals" / "results"


def labelled_predictions(engine: Engine, tenant_id: str, model_name: str = "m1_debit_failure") -> list[dict[str, Any]]:
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text(
            "SELECT p.prediction_id, p.subject_id, p.score, p.model_version, p.features_hash, "
            "(l.value->>'value')::boolean AS failed FROM ai.predictions p JOIN ai.labels l "
            "ON l.tenant_id=p.tenant_id AND l.prediction_id=p.prediction_id AND l.label_name='debit_failed' "
            "WHERE p.tenant_id=:t AND p.model_name=:m"), {"t": tenant_id, "m": model_name}).all()
    return [dict(r._mapping) for r in rows]


def evaluate_live(engine: Engine, tenant_id: str, write: bool = True) -> dict[str, Any]:
    rows = labelled_predictions(engine, tenant_id)
    n = len(rows)
    report: dict[str, Any] = {"tenant_id": tenant_id, "model": "m1_debit_failure", "n_labelled": n,
                              "data_source": "live_tenant"}
    if n:
        y = [1.0 if r["failed"] else 0.0 for r in rows]
        p = [float(r["score"]) for r in rows]
        report["brier"] = sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True)) / n
        report["mean_predicted"] = sum(p) / n
        report["observed_rate"] = sum(y) / n
        report["model_versions"] = sorted({r["model_version"] for r in rows})
        report["note"] = "too few labels for reliable metrics" if n < 200 else "ok"
    if write:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"live_eval_{tenant_id}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
