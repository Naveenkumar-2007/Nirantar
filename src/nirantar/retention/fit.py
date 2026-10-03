"""Tenant retention fit (P4, ADR-0014): sBG tenure model (→ M6 cold-start prior + CLV), churn-type rule (→ M13 prior),
subscriber values written to gold.subscription_value. Stored in ai.retention_fits with its diagnostics."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import numpy as np
import pyarrow as pa
from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.data.lake import Lake
from nirantar.data.lifecycle import TABLE as LIFECYCLE
from nirantar.db.session import tenant_tx
from nirantar.features.offline import TRAINING_TABLE as M1_TABLE
from nirantar.features.offline import build_training_set
from nirantar.ml.churn import RuleTypePrior, build_m13_training_set
from nirantar.ml.clv import value_subscribers
from nirantar.settings import service as settings
from nirantar.settings.schema import Retention

VALUE_TABLE = "gold.subscription_value"
MIN_SUBSCRIBERS, MIN_CHURNED = 30, 5          # below this the sBG fit is not attempted (reported, not guessed)


def fit_retention(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime) -> dict[str, Any]:
    with tenant_tx(tenant_id, engine) as c:
        cfg = settings.model(c, tenant_id, "retention", Retention)
    lc = lake.read(LIFECYCLE, tenant_id)
    payers = lc[lc.paid_cycles >= 1] if not lc.empty else lc
    churned = int((payers.status == "churned").sum()) if len(payers) else 0
    report: dict[str, Any] = {"subscribers": len(payers), "churned": churned}
    sbg: dict[str, float] | None = None
    if len(payers) >= MIN_SUBSCRIBERS and churned >= MIN_CHURNED:
        values, fit = value_subscribers(lc, annual_discount_rate=cfg.annual_discount_rate)
        sbg = {"alpha": fit["alpha"], "beta": fit["beta"]}
        report.update({"sbg": fit, "active_valued": len(values), "clv_total_minor": int(values.clv_minor.sum()),
                       "clv_mean_minor": float(values.clv_minor.mean()) if len(values) else 0.0})
        schema = pa.schema([("tenant_id", pa.string()), ("entity_id", pa.string()), ("paid_periods", pa.int64()),
                            ("p_renew_next", pa.float64()), ("expected_residual_payments", pa.float64()),
                            ("clv_minor", pa.int64()), ("period_days", pa.float64())])
        lake.replace_tenant(VALUE_TABLE, tenant_id, pa.Table.from_pandas(values, schema=schema, preserve_index=False))
    else:
        report["sbg_note"] = f"needs ≥{MIN_SUBSCRIBERS} paying subscribers and ≥{MIN_CHURNED} churns"
    type_rule: dict[str, float] | None = None
    if lake.read(M1_TABLE, tenant_id, ("tenant_id",)).empty:     # the rule needs feature snapshots; don't
        build_training_set(lake, tenant_id, now=now, source="fit")  # depend silently on pipeline step order
    m13 = build_m13_training_set(lake, tenant_id, now=now, source="fit")
    if len(m13) >= 20 and m13.label.nunique() == 2:
        rule = RuleTypePrior().fit(m13, m13.label.astype(int).to_numpy())
        type_rule = {"p_trouble": float(rule.p_trouble), "p_clean": float(rule.p_clean)}
        report["type_rule"] = {**type_rule, "rows": len(m13), "involuntary_share": float(np.mean(m13.label))}
    fit_id = new_id("rfit")
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ai.retention_fits (tenant_id, fit_id, fitted_at, sbg, type_rule, report) VALUES "
                       "(:t, :f, :at, CAST(:s AS jsonb), CAST(:r AS jsonb), CAST(:rep AS jsonb))"),
                  {"t": tenant_id, "f": fit_id, "at": now, "s": json.dumps(sbg) if sbg else None,
                   "r": json.dumps(type_rule) if type_rule else None, "rep": json.dumps(report, default=str)})
    return {"fit_id": fit_id, **report}
