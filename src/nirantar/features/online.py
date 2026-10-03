"""Online feature store (Redis): per-entity recent HISTORY + tenant recent events, not precomputed features.

At request time `features_for` runs the same `compute_features` as training, at as_of = now, on the stored
history — so serving can never compute a feature differently from training. Freshness = materialisation cadence
(hourly via Dagster, and after every pipeline run); `features_for` reports the history's age.

Keys (all tenant-scoped): nirantar:fs:{tenant}:{feature_set}:hist:{entity}, …:events, …:meta
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import redis

from nirantar.core.tenancy import validate_tenant_id
from nirantar.data.lake import Lake
from nirantar.features.compute import Cycle, TenantEvents, compute_features, tenure_stats
from nirantar.features.definitions import FEATURE_SET_ID, HISTORY_CAP
from nirantar.features.offline import cycles_with_entities, to_cycle

EVENTS_SPAN = timedelta(days=31)           # longest tenant window used by features is 30 days
TTL_S = 7 * 24 * 3600


def client() -> redis.Redis:
    return redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


@dataclass(frozen=True)
class OnlineFeatures:
    row: dict[str, Any]
    history_cycles: int
    materialized_at: datetime | None
    cold_start: bool                       # entity unknown to the store: features from tenant context only
    tenure: dict[str, float] | None = None  # inputs of the sBG churn prior (paid_periods, period_days)


class OnlineStore:
    def __init__(self, r: redis.Redis | None = None) -> None:
        self.r = r or client()

    @staticmethod
    def _k(tenant_id: str, *parts: str) -> str:
        validate_tenant_id(tenant_id)
        return ":".join(("nirantar:fs", tenant_id, FEATURE_SET_ID, *parts))

    def materialize(self, lake: Lake, tenant_id: str, *, now: datetime) -> dict[str, Any]:
        co = cycles_with_entities(lake, tenant_id)
        if co.empty:
            return {"entities": 0, "events": 0}
        co = co[co.first_attempt_failed.notna()].sort_values("first_attempt_at")
        pipe = self.r.pipeline(transaction=False)
        n = 0
        for entity, g in co.groupby("entity_id"):
            hist = [to_cycle(r).to_json() for r in g.tail(HISTORY_CAP).itertuples()]
            pipe.set(self._k(tenant_id, "hist", str(entity)), json.dumps(hist), ex=TTL_S)
            n += 1
        recent = co[co.first_attempt_at >= now - EVENTS_SPAN]
        events = TenantEvents.from_cycles([to_cycle(r) for r in recent.itertuples()])
        pipe.set(self._k(tenant_id, "events"), json.dumps(events.to_json()), ex=TTL_S)
        pipe.set(self._k(tenant_id, "meta"), json.dumps({"materialized_at": now.isoformat(), "entities": n,
                                                          "feature_set": FEATURE_SET_ID}), ex=TTL_S)
        pipe.execute()
        return {"entities": n, "events": len(events.times)}

    def features_for(self, tenant_id: str, entity_id: str, *, as_of: datetime, due_at: datetime, amount_minor: int,
                     method: str | None, bank: str | None) -> OnlineFeatures:
        raw_hist, raw_events, raw_meta = self.r.mget(self._k(tenant_id, "hist", entity_id),
                                                     self._k(tenant_id, "events"), self._k(tenant_id, "meta"))
        history = [Cycle.from_json(j) for j in json.loads(raw_hist)] if raw_hist else []
        events = TenantEvents.from_json(json.loads(raw_events)) if raw_events else TenantEvents()
        meta = json.loads(raw_meta) if raw_meta else {}
        row = compute_features(history, as_of=as_of, due_at=due_at, amount_minor=amount_minor, method=method,
                               bank=bank, tenant=events)
        at = datetime.fromisoformat(meta["materialized_at"]) if meta.get("materialized_at") else None
        return OnlineFeatures(row, len(history), at, cold_start=not history,
                              tenure=tenure_stats(history, as_of=as_of))

    def age(self, tenant_id: str, now: datetime | None = None) -> timedelta | None:
        raw = self.r.get(self._k(tenant_id, "meta"))
        if not raw:
            return None
        return (now or datetime.now(UTC)) - datetime.fromisoformat(json.loads(raw)["materialized_at"])
