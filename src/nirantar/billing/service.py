"""Billing domain services. Every function runs inside a caller-provided tenant transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.clock import Clock
from nirantar.core.crypto import encrypt, lookup_hash
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.stores import Outbox
from nirantar.verifier.payments import accrue_debit

SEGMENTS = frozenset({"subscription", "lending", "sip", "insurance", "b2b", "ecommerce"})


@dataclass(frozen=True)
class NewCustomer:
    external_ref: str
    display_name: str
    phone_e164: str | None
    email: str | None
    preferred_language: str = "en"
    segment: str = "subscription"
    consents: dict[str, Any] | None = None


def create_tenant(conn: Connection, tenant_id: str, name: str, settings: dict[str, Any] | None = None) -> None:
    conn.execute(
        text("INSERT INTO core.tenants (tenant_id, name, settings) VALUES (:t, :n, CAST(:s AS jsonb)) "
             "ON CONFLICT DO NOTHING"),
        {"t": tenant_id, "n": name, "s": json.dumps(settings or {})},
    )


def connect_provider(conn: Connection, tenant_id: str, provider: str, mode: str, secret_ref: str,
                     webhook_secret_ref: str) -> None:
    from nirantar.core.demo import DemoModeViolation, is_demo

    if is_demo() and provider != "mock":
        raise DemoModeViolation("the demo only connects the mock payment provider")
    conn.execute(
        text(
            "INSERT INTO core.provider_accounts (tenant_id, provider, mode, secret_ref, webhook_secret_ref) "
            "VALUES (:t, :p, :m, :s, :w) ON CONFLICT (tenant_id, provider, mode) DO UPDATE "
            "SET secret_ref = EXCLUDED.secret_ref, webhook_secret_ref = EXCLUDED.webhook_secret_ref"
        ),
        {"t": tenant_id, "p": provider, "m": mode, "s": secret_ref, "w": webhook_secret_ref},
    )


def create_customer(conn: Connection, tenant_id: str, c: NewCustomer) -> str:
    if c.segment not in SEGMENTS:
        raise ValueError(f"unknown segment {c.segment}")
    cid = new_id("cus")
    conn.execute(
        text(
            "INSERT INTO billing.customers (tenant_id, customer_id, external_ref, display_name, phone_enc, email_enc,"
            " contact_hash, preferred_language, consents, segment) VALUES (:t, :c, :x, :n, :pe, :ee, :h, :l, "
            "CAST(:cons AS jsonb), :seg)"
        ),
        {
            "t": tenant_id, "c": cid, "x": c.external_ref, "n": c.display_name,
            "pe": encrypt(c.phone_e164, tenant_id) if c.phone_e164 else None,
            "ee": encrypt(c.email, tenant_id) if c.email else None,
            "h": lookup_hash(c.phone_e164, tenant_id) if c.phone_e164 else None,
            "l": c.preferred_language, "cons": json.dumps(c.consents or {}), "seg": c.segment,
        },
    )
    return cid


def create_subscription(conn: Connection, tenant_id: str, customer_id: str, provider: str,
                        provider_subscription_id: str | None, amount: Money, interval: str = "monthly",
                        next_charge_on: date | None = None) -> str:
    sid = new_id("sub")
    conn.execute(
        text(
            "INSERT INTO billing.subscriptions (tenant_id, subscription_id, customer_id, provider, "
            "provider_subscription_id, amount_minor, currency, interval, status, next_charge_on) VALUES "
            "(:t, :s, :c, :p, :ps, :a, :cur, :i, 'active', :n)"
        ),
        {"t": tenant_id, "s": sid, "c": customer_id, "p": provider, "ps": provider_subscription_id,
         "a": amount.minor, "cur": amount.currency, "i": interval, "n": next_charge_on},
    )
    return sid


def schedule_debit(conn: Connection, tenant_id: str, subscription_id: str, scheduled_for: date,
                   now: datetime, clock: Clock | None = None) -> str:
    """Create the debit, accrue it in the ledger and emit debit.scheduled — atomically."""
    sub = conn.execute(
        text("SELECT customer_id, amount_minor, currency, provider FROM billing.subscriptions "
             "WHERE tenant_id=:t AND subscription_id=:s"),
        {"t": tenant_id, "s": subscription_id},
    ).one()
    debit_id = new_id("dbt")
    conn.execute(
        text(
            "INSERT INTO billing.debits (tenant_id, debit_id, subscription_id, customer_id, scheduled_for, "
            "amount_minor, currency, status) VALUES (:t, :d, :s, :c, :f, :a, :cur, 'scheduled')"
        ),
        {"t": tenant_id, "d": debit_id, "s": subscription_id, "c": sub.customer_id, "f": scheduled_for,
         "a": sub.amount_minor, "cur": sub.currency},
    )
    amount = Money(sub.amount_minor, sub.currency)
    accrue_debit(conn, tenant_id, debit_id, amount, sub.provider, now)
    Outbox(conn).add(make_event(
        event_type="subscription.debit_scheduled", version=1, tenant_id=tenant_id, subject_id=debit_id,
        payload={"debit_id": debit_id, "subscription_id": subscription_id, "customer_id": sub.customer_id,
                 "scheduled_for": scheduled_for.isoformat(), "amount_minor": amount.minor,
                 "currency": amount.currency, "provider": sub.provider},
        source="billing", occurred_at=now, clock=clock))
    return debit_id


def mark_attempting(conn: Connection, tenant_id: str, debit_id: str) -> None:
    conn.execute(
        text("UPDATE billing.debits SET status='attempting', updated_at=now() "
             "WHERE tenant_id=:t AND debit_id=:d AND status IN ('scheduled','notified','failed')"),
        {"t": tenant_id, "d": debit_id},
    )
