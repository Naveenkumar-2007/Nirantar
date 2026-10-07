"""Persistence for provider payments, debits and discrepancies (tenant-scoped connection)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.ids import new_id
from nirantar.payments.domain import PaymentStatus, ProviderPayment
from nirantar.payments.state import transition


def _jsonable(p: ProviderPayment) -> dict[str, Any]:
    d = asdict(p)
    d["amount"] = {"minor": p.amount.minor, "currency": p.amount.currency}
    d["status"] = p.status.value
    d["created_at"] = p.created_at.isoformat() if p.created_at else None
    d["notes"] = dict(p.notes)
    return d


def get_local_status(conn: Connection, tenant_id: str, provider: str, provider_payment_id: str) -> PaymentStatus | None:
    row = conn.execute(
        text("SELECT status FROM billing.payments WHERE tenant_id=:t AND provider=:p AND provider_payment_id=:id"),
        {"t": tenant_id, "p": provider, "id": provider_payment_id},
    ).one_or_none()
    return None if row is None else PaymentStatus(row.status)


def record_discrepancy(conn: Connection, tenant_id: str, p: ProviderPayment, kind: str,
                       local_state: dict[str, Any]) -> str:
    did = new_id("dsc")
    conn.execute(
        text(
            "INSERT INTO billing.discrepancies (tenant_id, discrepancy_id, provider, provider_payment_id, kind, "
            "local_state, provider_state) VALUES (:t, :d, :p, :id, :k, CAST(:l AS jsonb), CAST(:ps AS jsonb))"
        ),
        {"t": tenant_id, "d": did, "p": p.provider, "id": p.provider_payment_id, "k": kind,
         "l": json.dumps(local_state), "ps": json.dumps(_jsonable(p))},
    )
    return did


def upsert_payment(conn: Connection, tenant_id: str, p: ProviderPayment, *, debit_id: str | None = None,
                   customer_id: str | None = None, raw_event_id: str | None = None) -> tuple[PaymentStatus, bool]:
    """Apply provider truth to local state. Returns (resulting_status, changed)."""
    current = get_local_status(conn, tenant_id, p.provider, p.provider_payment_id)
    new_status, regression = transition(current, p.status)
    if regression:
        record_discrepancy(conn, tenant_id, p, "status_regression", {"status": current.value if current else None})
        return new_status, False
    if current is None:
        conn.execute(
            text(
                "INSERT INTO billing.payments (tenant_id, payment_id, provider, provider_payment_id, debit_id, "
                "customer_id, amount_minor, currency, status, method, error_code, error_reason, "
                "provider_created_at, raw_event_id, issuer) VALUES (:t, :pid, :p, :ppid, :d, :c, :a, :cur, :s, :m, "
                ":ec, :er, :pc, :raw, :iss) ON CONFLICT (tenant_id, provider, provider_payment_id) DO NOTHING"
            ),
            {"t": tenant_id, "pid": new_id("pmt"), "p": p.provider, "ppid": p.provider_payment_id, "d": debit_id,
             "c": customer_id, "a": p.amount.minor, "cur": p.amount.currency, "s": new_status.value,
             "m": p.method, "ec": p.error_code, "er": p.error_reason, "pc": p.created_at, "raw": raw_event_id,
             "iss": p.issuer},
        )
        return new_status, True
    if new_status == current:
        return current, False
    conn.execute(
        text(
            "UPDATE billing.payments SET status=:s, error_code=:ec, error_reason=:er, updated_at=now(), "
            "debit_id=coalesce(debit_id, :d) WHERE tenant_id=:t AND provider=:p AND provider_payment_id=:id"
        ),
        {"s": new_status.value, "ec": p.error_code, "er": p.error_reason, "d": debit_id, "t": tenant_id,
         "p": p.provider, "id": p.provider_payment_id},
    )
    return new_status, True
