"""Offline feature store: point-in-time-correct training sets from gold.charge_outcomes.

Each billing cycle becomes one training row: features computed by `compute_features` at as_of = due − LEAD from
the entity's history (the function itself hides anything not knowable at as_of), label = first attempt failed.
The set is written to `gold.training_<model>` so every trained model can point at the exact rows it used
(Iceberg snapshot id recorded in the model card).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pyarrow as pa

from nirantar.data import gold
from nirantar.data.lake import Lake
from nirantar.features.compute import Cycle, TenantEvents, compute_features
from nirantar.features.definitions import CATEGORICAL, FEATURE_SET_ID, LEAD, NUMERIC

TRAINING_TABLE = "gold.training_m1"
META = ["tenant_id", "cycle_id", "entity_id", "as_of", "due_at", "label", "feature_set", "source"]


def _nn(v: Any) -> Any:
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT else v


def _ts(v: Any) -> datetime | None:
    v = _nn(v)
    return None if v is None else pd.Timestamp(v).to_pydatetime()


def _amount(v: Any) -> int:
    v = _nn(v)
    return 1 if v is None else int(v)


def to_cycle(r: Any) -> Cycle:
    return Cycle(_ts(r.first_attempt_at), None if _nn(r.first_attempt_failed) is None else bool(r.first_attempt_failed),
                 _nn(r.failure_category), None if _nn(r.amount_minor) is None else int(r.amount_minor), _ts(r.paid_at),
                 bool(r.recovered), _nn(r.days_to_recover), _nn(r.method), _nn(r.bank))


def entity_map(lake: Lake, tenant_id: str) -> dict[str, str]:
    """provider subscription id → canonical Nirantar subscription id (when the tenant's subscription is mapped)."""
    subs = lake.read("silver.subscriptions", tenant_id)
    if subs.empty:
        return {}
    m = subs.dropna(subset=["provider_subscription_id"])
    return dict(zip(m.provider_subscription_id, m.subscription_id, strict=True))


def cycles_with_entities(lake: Lake, tenant_id: str) -> pd.DataFrame:
    co = lake.read(gold.TABLE, tenant_id)
    if co.empty:
        return co
    mapping = entity_map(lake, tenant_id)
    key = co.subscription_id.map(lambda s: mapping.get(s, s) if isinstance(s, str) else None)
    return co.assign(entity_id=key.fillna(co.customer_id).fillna("cycle:" + co.cycle_id))


def build_training_set(lake: Lake, tenant_id: str, *, now: datetime, source: str) -> pd.DataFrame:
    co = cycles_with_entities(lake, tenant_id)
    columns = META + NUMERIC + CATEGORICAL
    if co.empty:
        return pd.DataFrame(columns=columns)
    co = co[co.first_attempt_failed.notna()]
    events = TenantEvents.from_cycles([to_cycle(r) for r in co.itertuples()])
    rows: list[dict[str, Any]] = []
    for entity, g in co.groupby("entity_id"):
        history = [to_cycle(r) for r in g.itertuples()]
        for r, cyc in zip(g.itertuples(), history, strict=True):
            due = _ts(r.due_at) or cyc.first_attempt_at
            if due is None or cyc.first_attempt_at is None:
                continue
            as_of = min(due, cyc.first_attempt_at) - LEAD
            if as_of >= now:
                continue
            feats = compute_features(history, as_of=as_of, due_at=due, amount_minor=_amount(r.amount_minor),
                                     method=cyc.method, bank=cyc.bank, tenant=events)
            rows.append({"tenant_id": tenant_id, "cycle_id": r.cycle_id, "entity_id": str(entity), "as_of": as_of,
                         "due_at": due, "label": bool(r.first_attempt_failed), "feature_set": FEATURE_SET_ID,
                         "source": source, **feats})
    df = pd.DataFrame(rows, columns=columns).sort_values("as_of").reset_index(drop=True)
    schema = pa.schema(
        [("tenant_id", pa.string()), ("cycle_id", pa.string()), ("entity_id", pa.string()),
         ("as_of", pa.timestamp("us", tz="UTC")), ("due_at", pa.timestamp("us", tz="UTC")), ("label", pa.bool_()),
         ("feature_set", pa.string()), ("source", pa.string())]
        + [(c, pa.float64()) for c in NUMERIC] + [(c, pa.string()) for c in CATEGORICAL])
    out = df.astype({c: "float64" for c in NUMERIC})
    lake.replace_tenant(TRAINING_TABLE, tenant_id, pa.Table.from_pandas(out, schema=schema, preserve_index=False))
    return out
