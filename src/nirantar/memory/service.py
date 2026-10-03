"""Memory service.

- Only keys declared in SCHEMAS can be written, each with a value schema, allowed sources and a consent basis.
- Writes never overwrite: a new record supersedes the previous one (history stays auditable).
- Reads return the latest non-superseded record per key, with provenance and confidence.
- Agents write through the `memory.remember` MCP tool; nothing writes ai.memory directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.ids import new_id


class PreferredLanguage(BaseModel):
    language: str = Field(pattern=r"^(en|hi|te|ta|kn|mr|bn|gu|ml|pa|or)$")


class BestContactHour(BaseModel):
    hour_local: int = Field(ge=0, le=23)


class StatedSalaryDay(BaseModel):
    day_of_month: int = Field(ge=1, le=31)


class PromiseToPay(BaseModel):
    debit_id: str
    promised_date: str | None = None


class OfferGrid(BaseModel):
    max_discount_bp: int = Field(ge=0, le=5000)
    allowed_offers: list[str] = []


class Playbook(BaseModel):
    summary: str = Field(max_length=2000)
    evidence_ref: str


@dataclass(frozen=True)
class KeySpec:
    scope: str
    schema: type[BaseModel]
    sources: frozenset[str]
    consent_basis: str


SCHEMAS: dict[str, KeySpec] = {
    "preferred_language": KeySpec("customer", PreferredLanguage, frozenset({"customer_message", "tenant_crm"}),
                                  "service_communication"),
    "best_contact_hour": KeySpec("customer", BestContactHour, frozenset({"observed_response", "customer_message"}),
                                 "service_communication"),
    "stated_salary_day": KeySpec("customer", StatedSalaryDay, frozenset({"customer_message"}), "customer_disclosed"),
    "promise_to_pay": KeySpec("episodic", PromiseToPay, frozenset({"customer_message", "voice_call"}),
                              "service_communication"),
    "offer_grid": KeySpec("merchant", OfferGrid, frozenset({"tenant_admin"}), "contract"),
    "winning_playbook": KeySpec("procedural", Playbook, frozenset({"weekly_outcome_review"}), "aggregate_outcomes"),
}


class MemoryError_(ValueError):
    pass


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    key: str
    value: dict[str, Any]
    source: str
    confidence: float
    provenance: dict[str, Any]
    created_at: datetime


def remember(conn: Connection, tenant_id: str, subject_id: str, key: str, value: dict[str, Any], *, source: str,
             confidence: float, provenance: dict[str, Any], now: datetime) -> str:
    spec = SCHEMAS.get(key)
    if spec is None:
        raise MemoryError_(f"memory key {key!r} is not declared")
    if source not in spec.sources:
        raise MemoryError_(f"source {source!r} may not write {key!r}")
    if not 0.0 <= confidence <= 1.0:
        raise MemoryError_("confidence must be in [0, 1]")
    try:
        clean = spec.schema.model_validate(value).model_dump()
    except ValidationError as exc:
        raise MemoryError_(f"invalid value for {key}: {exc}") from exc
    mid = new_id("mem")
    conn.execute(text("INSERT INTO ai.memory (tenant_id, memory_id, scope, subject_id, key, value, source, confidence, "
                      "provenance, consent_basis, created_at) VALUES (:t, :m, :s, :sub, :k, CAST(:v AS jsonb), :src, "
                      ":c, CAST(:p AS jsonb), :cb, :n)"),
                 {"t": tenant_id, "m": mid, "s": spec.scope, "sub": subject_id, "k": key, "v": json.dumps(clean),
                  "src": source, "c": confidence, "p": json.dumps(provenance), "cb": spec.consent_basis, "n": now})
    conn.execute(text("UPDATE ai.memory SET superseded_by=:m WHERE tenant_id=:t AND subject_id=:sub AND key=:k "
                      "AND memory_id<>:m AND superseded_by IS NULL"),
                 {"m": mid, "t": tenant_id, "sub": subject_id, "k": key})
    return mid


def recall(conn: Connection, tenant_id: str, subject_id: str) -> dict[str, MemoryRecord]:
    rows = conn.execute(text("SELECT memory_id, key, value, source, confidence, provenance, created_at FROM ai.memory "
                             "WHERE tenant_id=:t AND subject_id=:s AND superseded_by IS NULL"),
                        {"t": tenant_id, "s": subject_id}).all()
    return {r.key: MemoryRecord(r.memory_id, r.key, dict(r.value), r.source, float(r.confidence), dict(r.provenance),
                                r.created_at) for r in rows}


def forget_subject(conn: Connection, tenant_id: str, subject_id: str, now: datetime) -> int:
    """DPDP erasure request: supersede every record for the subject with a tombstone (history of the erasure
    itself is kept; the values are removed)."""
    res = conn.execute(text("UPDATE ai.memory SET value='{}'::jsonb, provenance = provenance || "
                            "jsonb_build_object('erased_at', CAST(:n AS text)) WHERE tenant_id=:t AND subject_id=:s"),
                       {"n": now.isoformat(), "t": tenant_id, "s": subject_id})
    return int(res.rowcount or 0)
