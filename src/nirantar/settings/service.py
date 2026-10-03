"""Per-tenant settings: versioned, validated, audited (P1, ADR-0011).

- A namespace's effective value is its latest stored version, or the platform default (version 0).
- Every change stores the FULL new document as a new version (append-only table), writes a hash-chained audit
  record and emits `config.settings_changed` through the outbox — all in the caller's transaction.
- Optimistic concurrency: callers pass the version they edited; a stale write raises SettingsConflict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.audit.chain import AuditChain
from nirantar.contracts.events import make_event
from nirantar.core.clock import FixedClock
from nirantar.core.errors import NirantarError
from nirantar.db.stores import Outbox, SqlAuditStore
from nirantar.policy.engine import TenantPolicyConfig
from nirantar.settings.schema import NAMESPACES, Policy, platform_defaults
from nirantar.settings.schema import validate as validate_value


class SettingsConflict(NirantarError):
    pass


@dataclass(frozen=True)
class Versioned:
    namespace: str
    version: int                   # 0 = platform default (never edited by this tenant)
    value: dict[str, Any]
    changed_by: str
    reason: str
    created_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "version": self.version, "value": self.value,
                "changed_by": self.changed_by, "reason": self.reason, "created_at": self.created_at,
                "source": "platform_default" if self.version == 0 else "tenant"}


def _json(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else json.loads(v)


def default(namespace: str) -> Versioned:
    return Versioned(namespace, 0, dict(platform_defaults()["namespaces"][namespace]), "platform",
                     f"platform default {platform_defaults()['version']}", None)


def get(conn: Connection, tenant_id: str, namespace: str) -> Versioned:
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown settings namespace {namespace!r}")
    row = conn.execute(text("SELECT version, value, changed_by, reason, created_at FROM config.settings_versions "
                            "WHERE tenant_id=:t AND namespace=:n ORDER BY version DESC LIMIT 1"),
                       {"t": tenant_id, "n": namespace}).one_or_none()
    if row is None:
        return default(namespace)
    return Versioned(namespace, row.version, _json(row.value), row.changed_by, row.reason, row.created_at)


def model[M: BaseModel](conn: Connection, tenant_id: str, namespace: str, cls: type[M]) -> M:
    parsed = validate_value(namespace, get(conn, tenant_id, namespace).value)
    assert isinstance(parsed, cls)
    return parsed


def update(conn: Connection, tenant_id: str, namespace: str, value: dict[str, Any], *, actor: str, reason: str,
           now: datetime, expected_version: int | None) -> Versioned:
    if not reason.strip():
        raise ValueError("a reason is required for every settings change")
    parsed = validate_value(namespace, value)      # raises ValueError/ValidationError on bad input
    # Serialise concurrent writers of the same tenant+namespace, then check the version they edited.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"settings:{tenant_id}:{namespace}"})
    cur = get(conn, tenant_id, namespace)
    if expected_version is not None and expected_version != cur.version:
        raise SettingsConflict(f"{namespace} changed since version {expected_version} (now {cur.version})")
    doc = parsed.model_dump(mode="json")
    new_version = cur.version + 1
    conn.execute(text("INSERT INTO config.settings_versions (tenant_id, namespace, version, value, changed_by, "
                      "reason, created_at) VALUES (:t, :n, :v, CAST(:val AS jsonb), :by, :r, :at)"),
                 {"t": tenant_id, "n": namespace, "v": new_version, "val": json.dumps(doc), "by": actor,
                  "r": reason.strip()[:500], "at": now})
    AuditChain(SqlAuditStore(conn), FixedClock(now)).append(tenant_id, actor, "settings.changed", {
        "namespace": namespace, "from_version": cur.version, "to_version": new_version, "value": doc})
    Outbox(conn).add(make_event(event_type="config.settings_changed", version=1, tenant_id=tenant_id,
                                subject_id=f"{namespace}", payload={"namespace": namespace, "version": new_version},
                                source="settings", occurred_at=now))
    return Versioned(namespace, new_version, doc, actor, reason.strip()[:500], now)


def history(conn: Connection, tenant_id: str, namespace: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(text("SELECT version, value, changed_by, reason, created_at FROM config.settings_versions "
                             "WHERE tenant_id=:t AND namespace=:n ORDER BY version DESC LIMIT :l"),
                        {"t": tenant_id, "n": namespace, "l": limit}).all()
    out = [Versioned(namespace, r.version, _json(r.value), r.changed_by, r.reason, r.created_at).as_dict()
           for r in rows]
    return [*out, default(namespace).as_dict()] if len(out) < limit else out


def all_settings(conn: Connection, tenant_id: str) -> dict[str, Any]:
    return {ns: {**get(conn, tenant_id, ns).as_dict(), "default": default(ns).value} for ns in NAMESPACES}


def policy_config(conn: Connection, tenant_id: str) -> TenantPolicyConfig:
    """Effective Compliance Guardian config for a tenant (platform default tightened by tenant settings)."""
    return model(conn, tenant_id, "policy", Policy).config()
