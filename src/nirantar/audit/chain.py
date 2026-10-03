"""Per-tenant hash-chained audit log (BB-§36).

Each record stores the hash of its data and the hash of the previous record, so
altering, deleting or reordering any record breaks verification. Raw PII must
not be passed in `data`; pass references or hashes instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from nirantar.core.canonical import sha256_hex
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.errors import AuditChainBroken
from nirantar.core.tenancy import validate_tenant_id

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditRecord:
    tenant_id: str
    seq: int
    at: datetime
    actor: str  # e.g. "agent:debit_strategist", "user:usr_...", "system:verifier"
    action: str  # e.g. "tool.call", "policy.decision", "approval.granted"
    data_hash: str
    prev_hash: str
    hash: str

    @staticmethod
    def compute_hash(
        tenant_id: str, seq: int, at: datetime, actor: str, action: str, data_hash: str, prev_hash: str
    ) -> str:
        return sha256_hex(
            {
                "tenant_id": tenant_id,
                "seq": seq,
                "at": at,
                "actor": actor,
                "action": action,
                "data_hash": data_hash,
                "prev_hash": prev_hash,
            }
        )


class AuditStore(Protocol):
    def last(self, tenant_id: str) -> AuditRecord | None: ...
    def append(self, record: AuditRecord) -> None: ...
    def all(self, tenant_id: str) -> list[AuditRecord]: ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._records: dict[str, list[AuditRecord]] = {}

    def last(self, tenant_id: str) -> AuditRecord | None:
        records = self._records.get(tenant_id)
        return records[-1] if records else None

    def append(self, record: AuditRecord) -> None:
        self._records.setdefault(record.tenant_id, []).append(record)

    def all(self, tenant_id: str) -> list[AuditRecord]:
        return list(self._records.get(tenant_id, []))


class AuditChain:
    def __init__(self, store: AuditStore, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or SystemClock()

    def append(self, tenant_id: str, actor: str, action: str, data: Any) -> AuditRecord:
        validate_tenant_id(tenant_id)
        last = self._store.last(tenant_id)
        seq = 1 if last is None else last.seq + 1
        prev_hash = GENESIS if last is None else last.hash
        at = self._clock.now()
        data_hash = sha256_hex(data)
        record = AuditRecord(
            tenant_id=tenant_id,
            seq=seq,
            at=at,
            actor=actor,
            action=action,
            data_hash=data_hash,
            prev_hash=prev_hash,
            hash=AuditRecord.compute_hash(tenant_id, seq, at, actor, action, data_hash, prev_hash),
        )
        self._store.append(record)
        return record

    def verify(self, tenant_id: str) -> int:
        """Return the number of verified records; raise AuditChainBroken on any tampering."""
        prev = GENESIS
        records = self._store.all(tenant_id)
        for expected_seq, rec in enumerate(records, start=1):
            if rec.seq != expected_seq:
                raise AuditChainBroken(f"sequence gap at {expected_seq}")
            if rec.prev_hash != prev:
                raise AuditChainBroken(f"prev_hash mismatch at seq {rec.seq}")
            recomputed = AuditRecord.compute_hash(
                rec.tenant_id, rec.seq, rec.at, rec.actor, rec.action, rec.data_hash, rec.prev_hash
            )
            if recomputed != rec.hash:
                raise AuditChainBroken(f"hash mismatch at seq {rec.seq}")
            prev = rec.hash
        return len(records)
