"""Postgres implementations of the foundation store protocols.

Each store is bound to a Connection opened by `tenant_tx`, so RLS applies to
every statement. Stores never set tenant context themselves.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.audit.chain import AuditRecord
from nirantar.contracts.events import EventEnvelope
from nirantar.core.canonical import sha256_hex
from nirantar.core.errors import IdempotencyConflict
from nirantar.core.money import Money
from nirantar.ledger.ledger import Account, AccountType, JournalEntry, Line


class SqlLedgerStore:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def get_account(self, tenant_id: str, code: str) -> Account | None:
        row = self._c.execute(
            text("SELECT tenant_id, code, type, currency FROM ledger.accounts WHERE tenant_id=:t AND code=:c"),
            {"t": tenant_id, "c": code},
        ).one_or_none()
        return None if row is None else Account(row.tenant_id, row.code, AccountType(row.type), row.currency)

    def add_account(self, account: Account) -> None:
        self._c.execute(
            text("INSERT INTO ledger.accounts (tenant_id, code, type, currency) VALUES (:t, :c, :ty, :cur)"),
            {"t": account.tenant_id, "c": account.code, "ty": account.type.value, "cur": account.currency},
        )

    _ENTRY_QUERIES: ClassVar[Mapping[str | None, str]] = {
        col: (
            "SELECT entry_id, idempotency_key, memo, effective_at, posted_at, provider_ref, reverses, body_hash "
            "FROM ledger.entries WHERE tenant_id=:t"
            + (f" AND {col}=:v" if col else "")
            + " ORDER BY posted_at, entry_id"
        )
        for col in (None, "idempotency_key", "entry_id", "reverses")
    }

    def _load(self, tenant_id: str, column: str | None = None, value: str | None = None) -> list[JournalEntry]:
        # Only pre-built queries from a fixed allow-list are executed; values are always bound.
        query = self._ENTRY_QUERIES[column]
        rows = self._c.execute(text(query), {"t": tenant_id, "v": value}).all()
        out = []
        for r in rows:
            lines = self._c.execute(
                text(
                    "SELECT account_code, side, amount_minor, currency FROM ledger.lines "
                    "WHERE tenant_id=:t AND entry_id=:e ORDER BY line_no"
                ),
                {"t": tenant_id, "e": r.entry_id},
            ).all()
            out.append(
                JournalEntry(
                    entry_id=r.entry_id,
                    tenant_id=tenant_id,
                    idempotency_key=r.idempotency_key,
                    lines=tuple(
                        Line(
                            ln.account_code,
                            debit=Money(ln.amount_minor, ln.currency) if ln.side == "D" else None,
                            credit=Money(ln.amount_minor, ln.currency) if ln.side == "C" else None,
                        )
                        for ln in lines
                    ),
                    memo=r.memo,
                    effective_at=r.effective_at,
                    posted_at=r.posted_at,
                    provider_ref=r.provider_ref,
                    reverses=r.reverses,
                    body_hash=r.body_hash,
                )
            )
        return out

    def find_by_key(self, tenant_id: str, key: str) -> JournalEntry | None:
        found = self._load(tenant_id, "idempotency_key", key)
        return found[0] if found else None

    def get_entry(self, tenant_id: str, entry_id: str) -> JournalEntry | None:
        found = self._load(tenant_id, "entry_id", entry_id)
        return found[0] if found else None

    def find_reversal_of(self, tenant_id: str, entry_id: str) -> JournalEntry | None:
        found = self._load(tenant_id, "reverses", entry_id)
        return found[0] if found else None

    def insert_entry(self, entry: JournalEntry) -> None:
        self._c.execute(
            text(
                "INSERT INTO ledger.entries (tenant_id, entry_id, idempotency_key, memo, effective_at, "
                "posted_at, provider_ref, reverses, body_hash) VALUES (:t, :e, :k, :m, :ea, :pa, :pr, :rv, :bh)"
            ),
            {
                "t": entry.tenant_id, "e": entry.entry_id, "k": entry.idempotency_key, "m": entry.memo,
                "ea": entry.effective_at, "pa": entry.posted_at, "pr": entry.provider_ref,
                "rv": entry.reverses, "bh": entry.body_hash,
            },
        )
        for i, ln in enumerate(entry.lines, start=1):
            self._c.execute(
                text(
                    "INSERT INTO ledger.lines (tenant_id, entry_id, line_no, account_code, side, amount_minor, "
                    "currency) VALUES (:t, :e, :n, :a, :s, :amt, :cur)"
                ),
                {
                    "t": entry.tenant_id, "e": entry.entry_id, "n": i, "a": ln.account_code,
                    "s": "D" if ln.debit is not None else "C", "amt": ln.amount.minor,
                    "cur": ln.amount.currency,
                },
            )

    def entries(self, tenant_id: str) -> list[JournalEntry]:
        return self._load(tenant_id)

    def account_totals(self, tenant_id: str, code: str) -> tuple[int, int]:
        row = self._c.execute(
            text(
                "SELECT coalesce(sum(amount_minor) FILTER (WHERE side='D'),0) AS d, "
                "coalesce(sum(amount_minor) FILTER (WHERE side='C'),0) AS c "
                "FROM ledger.lines WHERE tenant_id=:t AND account_code=:a"
            ),
            {"t": tenant_id, "a": code},
        ).one()
        return int(row.d), int(row.c)


class SqlAuditStore:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def last(self, tenant_id: str) -> AuditRecord | None:
        # Serialise concurrent appends per tenant with a transaction-scoped advisory lock.
        # (The app role has no UPDATE grant on audit tables, so FOR UPDATE is not available;
        # the (tenant_id, seq) primary key is the final guard against forks.)
        self._c.execute(text("SELECT pg_advisory_xact_lock(hashtext('audit:' || :t))"), {"t": tenant_id})
        row = self._c.execute(
            text("SELECT * FROM audit.records WHERE tenant_id=:t ORDER BY seq DESC LIMIT 1"),
            {"t": tenant_id},
        ).one_or_none()
        return None if row is None else self._to_record(row)

    def append(self, record: AuditRecord) -> None:
        self._c.execute(
            text(
                "INSERT INTO audit.records (tenant_id, seq, at, actor, action, data_hash, prev_hash, hash) "
                "VALUES (:t, :s, :at, :ac, :an, :d, :p, :h)"
            ),
            {
                "t": record.tenant_id, "s": record.seq, "at": record.at, "ac": record.actor,
                "an": record.action, "d": record.data_hash, "p": record.prev_hash, "h": record.hash,
            },
        )

    def all(self, tenant_id: str) -> list[AuditRecord]:
        rows = self._c.execute(
            text("SELECT * FROM audit.records WHERE tenant_id=:t ORDER BY seq"), {"t": tenant_id}
        ).all()
        return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(r: Any) -> AuditRecord:
        return AuditRecord(r.tenant_id, r.seq, r.at, r.actor, r.action, r.data_hash, r.prev_hash, r.hash)


class Outbox:
    """Transactional outbox: events are written in the same transaction as state changes,
    then relayed to Kafka by `nirantar.events.relay`. No dual-write inconsistency."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def add(self, event: EventEnvelope) -> None:
        self._c.execute(
            text(
                "INSERT INTO events.outbox (tenant_id, event_id, event_type, version, subject_id, envelope) "
                "VALUES (:t, :e, :ty, :v, :s, CAST(:env AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            {
                "t": event.tenant_id, "e": event.event_id, "ty": event.event_type, "v": event.version,
                "s": event.subject_id, "env": event.model_dump_json(),
            },
        )


class IdempotencyStore:
    """Nirantar-side idempotency (providers are inconsistent here; see ADR-0003)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def begin(self, tenant_id: str, scope: str, key: str, request: Any) -> dict[str, Any] | None:
        """Return the stored response if this exact request already completed.
        Raise IdempotencyConflict if the key was used with a different request.
        Return None if the caller should proceed (key is now reserved)."""
        req_hash = sha256_hex(request)
        inserted = self._c.execute(
            text(
                "INSERT INTO core.idempotency_keys (tenant_id, scope, key, request_hash) "
                "VALUES (:t, :s, :k, :h) ON CONFLICT DO NOTHING RETURNING key"
            ),
            {"t": tenant_id, "s": scope, "k": key, "h": req_hash},
        ).one_or_none()
        if inserted is not None:
            return None
        row = self._c.execute(
            text(
                "SELECT request_hash, response FROM core.idempotency_keys "
                "WHERE tenant_id=:t AND scope=:s AND key=:k FOR UPDATE"
            ),
            {"t": tenant_id, "s": scope, "k": key},
        ).one()
        if row.request_hash != req_hash:
            raise IdempotencyConflict(f"{scope}:{key} reused with a different request")
        return dict(row.response) if row.response is not None else {"status": "in_progress"}

    def complete(self, tenant_id: str, scope: str, key: str, response: dict[str, Any]) -> None:
        self._c.execute(
            text(
                "UPDATE core.idempotency_keys SET response = CAST(:r AS jsonb) "
                "WHERE tenant_id=:t AND scope=:s AND key=:k"
            ),
            {"t": tenant_id, "s": scope, "k": key, "r": json.dumps(response, default=str)},
        )
