"""Versioned, immutable, tenant-aware event envelope (BB-§12).

Topic = event_type prefix (e.g. "payment" for "payment.failed").
Partition key = tenant_id + subject_id so per-customer ordering holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nirantar.core.canonical import sha256_hex
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.ids import new_id
from nirantar.core.tenancy import validate_tenant_id

TOPICS = frozenset(
    {
        "payment",
        "mandate",
        "subscription",
        "dispute",
        "invoice",                       # B2B receivables (ADR-0025)
        "reply",
        "call",
        "bank",
        "outcome",
        "experiment",
        "compliance",
        "approval",
        "provider",
        "config",
    }
)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: str = Field(pattern=r"^[a-z]+(\.[a-z_]+)+$")
    version: int = Field(ge=1)
    tenant_id: str
    subject_id: str  # the entity this event is about (customer, subscription, payment...)
    occurred_at: datetime
    recorded_at: datetime
    trace_id: str
    correlation_id: str
    causation_id: str | None = None
    source: str  # producer, e.g. "webhook-ingress/razorpay"
    payload: dict[str, Any]
    payload_sha256: str

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, v: str) -> str:
        return validate_tenant_id(v)

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _check(self) -> EventEnvelope:
        if self.topic not in TOPICS:
            raise ValueError(f"unknown topic {self.topic!r}")
        if sha256_hex(self.payload) != self.payload_sha256:
            raise ValueError("payload hash mismatch")
        return self

    @property
    def topic(self) -> str:
        return self.event_type.split(".", 1)[0]

    @property
    def partition_key(self) -> str:
        return f"{self.tenant_id}:{self.subject_id}"


def make_event(
    *,
    event_type: str,
    version: int,
    tenant_id: str,
    subject_id: str,
    payload: dict[str, Any],
    source: str,
    occurred_at: datetime,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    clock: Clock | None = None,
) -> EventEnvelope:
    event_id = new_id("evt")
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        version=version,
        tenant_id=tenant_id,
        subject_id=subject_id,
        occurred_at=occurred_at,
        recorded_at=(clock or SystemClock()).now(),
        trace_id=trace_id or new_id("trc"),
        correlation_id=correlation_id or event_id,
        causation_id=causation_id,
        source=source,
        payload=payload,
        payload_sha256=sha256_hex(payload),
    )
