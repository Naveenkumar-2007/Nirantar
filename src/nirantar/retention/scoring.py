"""Daily churn-risk scoring of ACTIVE subscribers + delayed labelling of those predictions (P4, ADR-0014).

score_active: for each active subscription, features at now (online store, the shared feature function) →
  M6 P(churn in 60 days) and M13 P(payment-driven | churn) through the model router (champion / canary / shadow /
  transparent prior; no prior fitted yet → no score). Output: an at-risk list with a ROUTE:
    fix_payment     likely involuntary — mandate/payment-method repair, not a discount
    retention_offer likely voluntary — a retention conversation or offer (promotional consent required)
  Every score is logged in ai.predictions (subject = "<entity>@<date>") for later labelling.
label_predictions: once the outcome is knowable, writes ai.labels from the lifecycle table:
  churned_60d (M6: churned within 60 days after the prediction) and churn_involuntary (M13, for churned entities).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.data.lake import Lake
from nirantar.data.lifecycle import TABLE as LIFECYCLE
from nirantar.db.session import tenant_tx
from nirantar.features.online import OnlineStore
from nirantar.ml.churn import HORIZON
from nirantar.ml.router import ModelRouter
from nirantar.settings import service as settings
from nirantar.settings.schema import Retention


class _Row:
    """Attribute access over a lifecycle record (typed Any: values come from the lake)."""

    def __init__(self, rec: dict[Any, Any]) -> None:
        self.__dict__.update({str(k): v for k, v in rec.items()})

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def score_active(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime, store: OnlineStore,
                 router: ModelRouter) -> dict[str, Any]:
    lc = lake.read(LIFECYCLE, tenant_id)
    active = lc[lc.status == "active"] if not lc.empty else lc
    with tenant_tx(tenant_id, engine) as c:
        rule = settings.model(c, tenant_id, "retention", Retention).at_risk
    scored_rows: list[dict[str, Any]] = []
    unscored = 0
    for rec in active.to_dict("records"):
        r = _Row(rec)
        due = r.last_cycle_at.to_pydatetime() + timedelta(days=float(r.period_days))
        of = store.features_for(tenant_id, str(r.entity_id), as_of=now, due_at=max(due, now), method=None, bank=None,
                                amount_minor=int(r.last_amount_minor or 1))
        feats = {**of.row, **(of.tenure or {})}
        subject = f"{r.entity_id}@{now:%Y-%m-%d}"
        with tenant_tx(tenant_id, engine) as c:
            prev = c.execute(text("SELECT score, model_version FROM ai.predictions WHERE tenant_id=:t AND "
                                  "subject_id=:s AND model_name=:m AND output->>'role'='decision'"),
                             {"t": tenant_id, "s": subject, "m": "m6_churn"}).first()
            if prev is not None:                               # already scored today (idempotent re-runs)
                p_kind = c.execute(text("SELECT score FROM ai.predictions WHERE tenant_id=:t AND subject_id=:s AND "
                                        "model_name='m13_churn_type' AND output->>'role'='decision'"),
                                   {"t": tenant_id, "s": subject}).scalar_one_or_none()
                churn_score, churn_version = float(prev.score), str(prev.model_version)
            else:
                churn = router.score_model(c, tenant_id, "m6_churn", subject_id=subject,
                                           entity_id=str(r.entity_id), features=feats, now=now)
                kind = router.score_model(c, tenant_id, "m13_churn_type", subject_id=subject,
                                          entity_id=str(r.entity_id), features=feats, now=now)
                if churn is None:
                    unscored += 1
                    continue
                churn_score, churn_version = churn.score, churn.version
                p_kind = kind.score if kind is not None else None
        scored_rows.append({"entity_id": str(r.entity_id), "customer_id": r.customer_id, "p_churn_60d": churn_score,
                            "churn_model": churn_version, "p_payment_driven": p_kind,
                            "route": "fix_payment" if (p_kind is not None and p_kind >= 0.5) else "retention_offer",
                            "monthly_value_minor": int(r.monthly_value_minor)})
    average = sum(x["p_churn_60d"] for x in scored_rows) / len(scored_rows) if scored_rows else 0.0
    threshold = max(rule.min_churn_probability, rule.min_lift_over_average * average)
    at_risk = sorted((x for x in scored_rows if x["p_churn_60d"] >= threshold),
                     key=lambda x: -x["p_churn_60d"] * x["monthly_value_minor"])
    top = max((x["p_churn_60d"] for x in scored_rows), default=0.0)
    decided_by = sorted({x["churn_model"].split(":", 1)[0] for x in scored_rows})
    return {"active": len(active), "scored": len(scored_rows), "unscored": unscored, "average": average,
            "max": top, "threshold": threshold, "decided_by": decided_by, "at_risk": at_risk}


def label_predictions(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime) -> dict[str, int]:
    """Write the delayed labels for churn predictions whose outcome is now observable (source 'lifecycle')."""
    lc = lake.read(LIFECYCLE, tenant_id)
    if lc.empty:
        return {"churned_60d": 0, "churn_involuntary": 0}
    info = {str(rec["entity_id"]): _Row(rec) for rec in lc.to_dict("records")}
    written = {"churned_60d": 0, "churn_involuntary": 0}
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text(
            "SELECT DISTINCT p.subject_id, p.model_name, p.predicted_at FROM ai.predictions p WHERE p.tenant_id=:t "
            "AND p.model_name IN ('m6_churn','m13_churn_type') AND NOT EXISTS (SELECT 1 FROM ai.labels l WHERE "
            "l.tenant_id=p.tenant_id AND l.subject_id=p.subject_id AND l.label_name=CASE p.model_name WHEN "
            "'m6_churn' THEN 'churned_60d' ELSE 'churn_involuntary' END)"), {"t": tenant_id}).all()
        for r in rows:
            entity = r.subject_id.split("@", 1)[0]
            e = info.get(entity)
            if e is None:
                continue
            churned = e.status == "churned" and not pd.isna(e.churn_at)
            if r.model_name == "m6_churn":
                horizon_end = r.predicted_at + HORIZON
                if churned and e.churn_at.to_pydatetime() <= horizon_end:
                    value = e.churn_at.to_pydatetime() > r.predicted_at
                elif horizon_end <= now:
                    value = False
                else:
                    continue                                   # not observable yet
                name = "churned_60d"
            else:
                if not churned or e.churn_type not in ("involuntary", "voluntary"):
                    continue                                   # type only known once churned
                value, name = e.churn_type == "involuntary", "churn_involuntary"
            c.execute(text("INSERT INTO ai.labels (tenant_id, label_id, prediction_id, subject_id, label_name, value, "
                           "source, observed_at) VALUES (:t, :l, NULL, :s, :n, CAST(:v AS jsonb), 'lifecycle', :at)"),
                      {"t": tenant_id, "l": new_id("lbl"), "s": r.subject_id, "n": name,
                       "v": json.dumps({"value": bool(value)}), "at": now})
            written[name] += 1
    return written
