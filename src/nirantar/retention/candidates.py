"""Win-back candidate selection + randomisation (P4, ADR-0014).

Everything that decides WHO is eligible uses pre-treatment information only, and happens BEFORE random assignment
— otherwise the holdout comparison would be biased:
  churned within [min_days, max_days] · a Nirantar customer we can contact · WhatsApp AND promotional consent, not
  opted out · no revival case for the subscription in 90 days · per-customer offer limit · daily capacity (most
  valuable first, by sBG value-if-back).
Randomisation is stratified by churn type, each with its own experiment and holdout:
  involuntary (payment trouble): reminder arm only — a discount does not fix a failed mandate;
  voluntary (chose to leave): every offer arm in the tenant's retention settings.
Each selected subscriber gets a `revival` case whose summary holds the arm and the terms computed by code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.data.lake import Lake
from nirantar.data.lifecycle import TABLE as LIFECYCLE
from nirantar.db.session import tenant_tx
from nirantar.experiments import service as experiments
from nirantar.ml.clv import SBGFit
from nirantar.settings import service as settings
from nirantar.settings.schema import Retention, Winback

COOLDOWN = timedelta(days=90)
WINBACK_SEGMENTS = {"subscription", "b2b", "sip", "insurance"}   # never lending/MFI: loans are not "won back"


class _Row:
    """Attribute access over a lifecycle record (typed Any: values come from the lake)."""

    def __init__(self, rec: dict[Any, Any]) -> None:
        self.__dict__.update({str(k): v for k, v in rec.items()})

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _experiment(c: Connection, tenant_id: str, stratum: str, arms: list[str], holdout_bp: int) -> str:
    """Find or create the win-back experiment for a stratum and exact arm set (new arms → new experiment)."""
    key = hashlib.sha256(json.dumps([stratum, sorted(arms), holdout_bp]).encode()).hexdigest()[:8]
    name = f"winback-{stratum}-{key}"
    row = c.execute(text("SELECT experiment_id FROM experiments.experiments WHERE tenant_id=:t AND name=:n"),
                    {"t": tenant_id, "n": name}).scalar_one_or_none()
    if row:
        return str(row)
    share, rest = divmod(10_000 - holdout_bp, len(arms))
    bp = {a: share + (rest if i == 0 else 0) for i, a in enumerate(arms)}
    return experiments.create_experiment(c, tenant_id, name, bp, holdout_bp=holdout_bp)


def _case_id(entity_id: str, now: datetime) -> str:
    return "cas_rv" + hashlib.sha256(f"{entity_id}:{now:%Y%m}".encode()).hexdigest()[:20]


def select_winback(engine: Engine, lake: Lake, tenant_id: str, *, now: datetime) -> dict[str, Any]:
    lc = lake.read(LIFECYCLE, tenant_id)
    report: dict[str, Any] = {"created": [], "excluded": {}}
    if lc.empty:
        return report
    with tenant_tx(tenant_id, engine) as c:
        cfg = settings.model(c, tenant_id, "retention", Retention)
        fit = c.execute(text("SELECT sbg FROM ai.retention_fits WHERE tenant_id=:t ORDER BY fitted_at DESC LIMIT 1"),
                        {"t": tenant_id}).scalar_one_or_none()
    wb: Winback = cfg.winback
    lo, hi = pd.Timestamp(now - timedelta(days=wb.max_days_since_churn)), \
        pd.Timestamp(now - timedelta(days=wb.min_days_since_churn))
    pool = lc[(lc.status == "churned") & lc.churn_type.isin(["voluntary", "involuntary"])
              & (lc.churn_at >= lo) & (lc.churn_at <= hi)]
    excluded: dict[str, int] = {}

    def drop(reason: str, n: int) -> None:
        if n:
            excluded[reason] = excluded.get(reason, 0) + n

    sbg = SBGFit(float(fit["alpha"]), float(fit["beta"]), 0, 0, 0) if fit else None
    rows: list[dict[str, Any]] = []
    with tenant_tx(tenant_id, engine) as c:
        for rec in pool.to_dict("records"):
            r = _Row(rec)
            sub = c.execute(text("SELECT s.subscription_id, s.customer_id, s.amount_minor, c.consents, c.segment "
                                 "FROM billing.subscriptions s JOIN billing.customers c ON c.tenant_id=s.tenant_id "
                                 "AND c.customer_id=s.customer_id WHERE s.tenant_id=:t AND s.subscription_id=:s"),
                            {"t": tenant_id, "s": r.entity_id}).one_or_none()
            if sub is None:
                drop("not a Nirantar customer (no contact details)", 1)
                continue
            consents = sub.consents if isinstance(sub.consents, dict) else json.loads(sub.consents or "{}")
            if (sub.segment or "subscription") not in WINBACK_SEGMENTS:
                drop("segment not eligible for win-back", 1)
                continue
            if not consents.get("whatsapp") or not consents.get("promotional") or \
                    "whatsapp" in consents.get("opted_out", []):
                drop("no WhatsApp + promotional consent", 1)
                continue
            recent: int = c.execute(text("SELECT count(*) FROM ops.cases WHERE tenant_id=:t AND kind='revival' AND "
                                    "subject_id=:s AND opened_at > :since"),
                               {"t": tenant_id, "s": r.entity_id, "since": now - COOLDOWN}).scalar_one()
            offers: int = c.execute(text("SELECT count(*) FROM billing.offers WHERE tenant_id=:t AND "
                                         "customer_id=:c AND created_at > :since"),
                               {"t": tenant_id, "c": sub.customer_id, "since": now - timedelta(days=90)}).scalar_one()
            if recent or offers >= wb.max_offers_per_customer_90d:
                drop("contacted about win-back in the last 90 days", 1)
                continue
            n = max(1, int(r.paid_cycles))
            residual = sbg.residual_payments(n, (1 + cfg.annual_discount_rate) ** (r.period_days / 365) - 1) \
                if sbg else 1.0
            rows.append({"entity_id": r.entity_id, "customer_id": sub.customer_id, "type": r.churn_type,
                         "amount": int(sub.amount_minor), "value": residual * int(sub.amount_minor),
                         "churn_at": r.churn_at, "paid_cycles": n})
        rows.sort(key=lambda x: -x["value"])
        drop("over today's capacity", max(0, len(rows) - wb.daily_capacity))
        offers_by_id = {o.id: o for o in wb.offers}
        exps = {"voluntary": _experiment(c, tenant_id, "voluntary", list(offers_by_id), wb.holdout_bp),
                "involuntary": _experiment(c, tenant_id, "involuntary",
                                           [o.id for o in wb.offers if o.kind == "reminder"] or [wb.offers[0].id],
                                           wb.holdout_bp)}
        for x in rows[: wb.daily_capacity]:
            exp = exps[x["type"]]
            arm = experiments.assign(c, tenant_id, exp, x["customer_id"], now)
            offer = offers_by_id.get(arm)
            case_id = _case_id(x["entity_id"], now)
            summary = {"experiment_id": exp, "arm": arm, "stratum": x["type"],
                       "kind": offer.kind if offer else "holdout", "discount_pct": offer.discount_pct if offer else 0,
                       "list_amount_minor": x["amount"], "window_days": wb.window_days,
                       "churn_at": str(x["churn_at"]), "value_if_back_minor": round(x["value"]),
                       "paid_cycles": x["paid_cycles"]}
            inserted = c.execute(text(
                "INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, summary, opened_at) "
                "VALUES (:t, :c, 'revival', :s, :cu, 'open', CAST(:sum AS jsonb), :now) ON CONFLICT DO NOTHING "
                "RETURNING case_id"), {"t": tenant_id, "c": case_id, "s": x["entity_id"], "cu": x["customer_id"],
                                       "sum": json.dumps(summary), "now": now}).scalar_one_or_none()
            if inserted:
                report["created"].append({"case_id": case_id, "customer_id": x["customer_id"], **summary})
    report["excluded"] = excluded
    report["pool"] = len(pool)
    return report
