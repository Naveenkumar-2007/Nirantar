"""Gold `subscription_lifecycle`: who churned, when, and why — the label table for churn (M6), churn type (M13),
lifetime value (M15) and the revival loop (P4, ADR-0014).

Per canonical subscription (same entity key as the feature store):
- status   active (censored: still paying or not yet overdue) | churned | completed (a natural end — NOT churn)
- churn_type  involuntary (ended after a failed, unrecovered cycle or provider `halted`) | voluntary (last cycle
  paid, then cancelled / stopped)
- churn_at, churn_source: the provider's `ended_at` when it says cancelled/halted/expired/completed; otherwise
  inferred from silence — no attempt for LAPSE_PERIODS × the entity's billing period + GRACE after its last cycle;
  churn_at = the missed next due date (last cycle + period).
- monthly_value_minor: last amount normalised to 30 days (for CLV).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import pandas as pd
import pyarrow as pa

from nirantar.data.lake import Lake
from nirantar.features.offline import cycles_with_entities, entity_map

TABLE = "gold.subscription_lifecycle"
LAPSE_PERIODS = 1.5
GRACE = timedelta(days=7)
DEFAULT_PERIOD_DAYS = 30.0
ENDED = {"cancelled": "voluntary", "halted": "involuntary", "expired": None, "completed": "completed"}

SCHEMA = pa.schema([
    ("tenant_id", pa.string()), ("entity_id", pa.string()), ("customer_id", pa.string()),
    ("started_at", pa.timestamp("us", tz="UTC")), ("last_cycle_at", pa.timestamp("us", tz="UTC")),
    ("cycles", pa.int64()), ("paid_cycles", pa.int64()), ("failed_cycles", pa.int64()),
    ("period_days", pa.float64()), ("status", pa.string()), ("churn_type", pa.string()),
    ("churn_at", pa.timestamp("us", tz="UTC")), ("churn_source", pa.string()),
    ("last_amount_minor", pa.int64()), ("monthly_value_minor", pa.int64()), ("revenue_minor", pa.int64()),
    ("renewals", pa.int64()), ("as_of", pa.timestamp("us", tz="UTC")),
])


@dataclass(frozen=True)
class LifecycleResult:
    subscriptions: int
    active: int
    churned: int
    involuntary: int
    voluntary: int
    completed: int


def _provider_status(lake: Lake, tenant_id: str) -> dict[str, tuple[str, Any]]:
    """canonical entity → (provider status, ended_at) from provider + Nirantar subscription records."""
    out: dict[str, tuple[str, Any]] = {}
    mapping = entity_map(lake, tenant_id)
    ps = lake.read("silver.provider_subscriptions", tenant_id)
    for rec in ps.to_dict("records") if not ps.empty else []:
        sid = str(rec["subscription_id"])
        out[mapping.get(sid, sid)] = (str(rec["status"]), rec.get("ended_at"))
    ns = lake.read("silver.subscriptions", tenant_id)
    for rec in ns.to_dict("records") if not ns.empty else []:
        sid = str(rec["subscription_id"])
        if sid not in out and rec["status"] in ("cancelled", "halted", "completed"):
            out[sid] = (str(rec["status"]), None)
    return out


def build_lifecycle(lake: Lake, tenant_id: str, *, now: datetime) -> LifecycleResult:
    co = cycles_with_entities(lake, tenant_id)
    status_of = _provider_status(lake, tenant_id)
    rows: list[dict[str, Any]] = []
    if not co.empty:
        co = co[co.first_attempt_at.notna()].sort_values("first_attempt_at")
        for entity, g in co.groupby("entity_id"):
            times = list(g.first_attempt_at)
            gaps = [(b - a).total_seconds() / 86400 for a, b in pairwise(times)]
            period = float(pd.Series(gaps).median()) if gaps else DEFAULT_PERIOD_DAYS
            period = min(max(period, 6.0), 366.0)                  # weekly … yearly plans
            last = g.iloc[-1]
            last_at = last.first_attempt_at.to_pydatetime()
            last_unrecovered = bool(last.first_attempt_failed) and not bool(last.paid)
            pstatus, ended_at = status_of.get(str(entity), ("", None))
            status, ctype, churn_at, source = "active", None, None, None
            if pstatus in ENDED and ENDED[pstatus] == "completed":
                status, source = "completed", "provider"
                churn_at = ended_at if ended_at is not None and not pd.isna(ended_at) else None
            elif pstatus in ENDED:
                status, source = "churned", "provider"
                ctype = ENDED[pstatus] or ("involuntary" if last_unrecovered else "voluntary")
                if pstatus == "cancelled" and last_unrecovered:
                    ctype = "involuntary"          # cancelled right after an unpaid failure = payment-driven
                churn_at = ended_at if ended_at is not None and not pd.isna(ended_at) \
                    else last_at + timedelta(days=period)
            elif now - last_at > timedelta(days=LAPSE_PERIODS * period) + GRACE:
                status, source = "churned", "inferred"
                ctype = "involuntary" if last_unrecovered else "voluntary"
                churn_at = last_at + timedelta(days=period)
            paid = g[g.paid.astype(bool)]
            amount = int(last.amount_minor) if not pd.isna(last.amount_minor) else 0
            rows.append({
                "tenant_id": tenant_id, "entity_id": str(entity),
                "customer_id": None if pd.isna(last.customer_id) else str(last.customer_id),
                "started_at": times[0], "last_cycle_at": last_at, "cycles": len(g), "paid_cycles": len(paid),
                "failed_cycles": int(g.first_attempt_failed.fillna(False).astype(bool).sum()),
                "period_days": period, "status": status, "churn_type": ctype, "churn_at": churn_at,
                "churn_source": source, "last_amount_minor": amount,
                "monthly_value_minor": round(amount * 30.0 / period), "revenue_minor": int(paid.amount_minor.sum()),
                "renewals": max(0, len(paid) - 1), "as_of": now,
            })
    df = pd.DataFrame(rows, columns=SCHEMA.names)
    lake.replace_tenant(TABLE, tenant_id, pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False))
    churned = df[df.status == "churned"] if not df.empty else df
    return LifecycleResult(len(df), int((df.status == "active").sum()) if not df.empty else 0, len(churned),
                           int((churned.churn_type == "involuntary").sum()) if len(churned) else 0,
                           int((churned.churn_type == "voluntary").sum()) if len(churned) else 0,
                           int((df.status == "completed").sum()) if not df.empty else 0)
