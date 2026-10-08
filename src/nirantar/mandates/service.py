"""Mandate state from provider truth.

Discovery: a payment carries the token the provider debited (Razorpay `payment.token_id`) and the provider customer.
The customer's token list (Razorpay `GET /customers/{id}/tokens`) gives rail, status, limit and expiry; the mandate
is upserted and linked to the subscription that was charged.
Updates: token webhooks carry only the token entity. Its status is confirmed against the customer's token list; a
token missing from that list (Razorpay omits expired, rejected and unused tokens) is accepted only as an END state
(failed / revoked / expired) — a webhook can never make a mandate healthier than the provider list says it is.
Every change emits `mandate.updated` through the outbox.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.clock import Clock
from nirantar.db.stores import Outbox
from nirantar.payments.domain import PaymentProvider, ProviderMandate, ProviderPayment, ProviderUnavailable

END_STATES = frozenset({"failed", "revoked", "expired"})


@dataclass(frozen=True)
class MandateChange:
    mandate_id: str
    status: str
    previous: str | None
    changed: bool


def mandate_id_for(tenant_id: str, provider: str, token: str) -> str:
    return "mdt_" + hashlib.sha256(f"{tenant_id}:{provider}:{token}".encode()).hexdigest()[:24]


def _lister(provider: PaymentProvider) -> Any:
    return getattr(provider, "list_mandates", None)


def upsert_mandate(conn: Connection, tenant_id: str, m: ProviderMandate, customer_id: str, now: datetime,
                   *, raw_event_id: str | None = None, clock: Clock | None = None) -> MandateChange:
    # a token already on record (e.g. imported or registered before discovery existed) keeps its mandate id
    known = conn.execute(text("SELECT mandate_id FROM billing.mandates WHERE tenant_id=:t AND provider=:p AND "
                              "provider_token_id=:tok"),
                         {"t": tenant_id, "p": m.provider, "tok": m.provider_token_id}).scalar_one_or_none()
    mid = known or mandate_id_for(tenant_id, m.provider, m.provider_token_id)
    prev = conn.execute(text("SELECT status FROM billing.mandates WHERE tenant_id=:t AND mandate_id=:m"),
                        {"t": tenant_id, "m": mid}).scalar_one_or_none()
    max_minor = m.max_amount.minor if m.max_amount else None
    conn.execute(text(
        "INSERT INTO billing.mandates (tenant_id, mandate_id, customer_id, provider, provider_token_id, rail, "
        "max_amount_minor, currency, status, valid_until, provider_customer_ref, failure_reason, last_verified_at) "
        "VALUES (:t, :m, :c, :p, :tok, :rail, :max, :cur, :s, :vu, :pc, :fr, :now) "
        "ON CONFLICT (tenant_id, mandate_id) DO UPDATE SET status=EXCLUDED.status, rail=EXCLUDED.rail, "
        "max_amount_minor=COALESCE(:max, billing.mandates.max_amount_minor), valid_until=EXCLUDED.valid_until, "
        "provider_customer_ref=COALESCE(EXCLUDED.provider_customer_ref, billing.mandates.provider_customer_ref), "
        "failure_reason=EXCLUDED.failure_reason, last_verified_at=EXCLUDED.last_verified_at, updated_at=:now"),
        {"t": tenant_id, "m": mid, "c": customer_id, "p": m.provider, "tok": m.provider_token_id,
         "rail": m.rail if m.rail in ("upi_autopay", "emandate", "card", "nach") else "other", "max": max_minor,
         "cur": m.max_amount.currency if m.max_amount else "INR", "s": m.status,
         "vu": m.valid_until.date() if m.valid_until else None, "pc": m.customer_ref, "fr": m.failure_reason,
         "now": now})
    changed = prev != m.status
    if changed:
        Outbox(conn).add(make_event(
            event_type="mandate.updated", version=1, tenant_id=tenant_id, subject_id=mid,
            payload={"mandate_id": mid, "customer_id": customer_id, "status": m.status, "previous_status": prev,
                     "rail": m.rail, "failure_reason": m.failure_reason},
            source="mandates/service", occurred_at=now, causation_id=raw_event_id, clock=clock))
    return MandateChange(mid, m.status, prev, changed)


def discover_from_payment(conn: Connection, tenant_id: str, provider: PaymentProvider, p: ProviderPayment,
                          customer_id: str | None, now: datetime, raw_event_id: str | None = None,
                          clock: Clock | None = None) -> str | None:
    """Link the debited token to its customer and subscription. Returns the mandate id, or None if unknown."""
    if not (p.token_ref and p.customer_ref and customer_id):
        return None
    mid = mandate_id_for(tenant_id, p.provider, p.token_ref)
    known = conn.execute(text("SELECT 1 FROM billing.mandates WHERE tenant_id=:t AND mandate_id=:m"),
                         {"t": tenant_id, "m": mid}).scalar_one_or_none()
    if not known:
        lister = _lister(provider)
        if lister is None:
            return None
        match = next((m for m in lister(p.customer_ref) if m.provider_token_id == p.token_ref), None)
        if match is None:
            return None                      # not listable (expired/unused): nothing verified to record
        upsert_mandate(conn, tenant_id, match, customer_id, now, raw_event_id=raw_event_id, clock=clock)
    if p.subscription_ref:
        conn.execute(text("UPDATE billing.subscriptions SET mandate_id=:m WHERE tenant_id=:t AND provider=:p AND "
                          "provider_subscription_id=:s AND mandate_id IS DISTINCT FROM :m"),
                     {"m": mid, "t": tenant_id, "p": p.provider, "s": p.subscription_ref})
    return mid


def apply_token_event(conn: Connection, tenant_id: str, provider: PaymentProvider, hinted: ProviderMandate,
                      now: datetime, raw_event_id: str | None = None, clock: Clock | None = None) -> str:
    """A token webhook → verified mandate state. Returns an outcome label."""
    mid = mandate_id_for(tenant_id, provider.name, hinted.provider_token_id)
    row = conn.execute(text("SELECT customer_id, provider_customer_ref, status FROM billing.mandates WHERE "
                            "tenant_id=:t AND mandate_id=:m"), {"t": tenant_id, "m": mid}).one_or_none()
    if row is None:
        return "unknown_token"       # linked later, when a payment shows which customer it belongs to
    lister = _lister(provider)
    listed = None
    if lister is not None and row.provider_customer_ref:
        listed = next((m for m in lister(row.provider_customer_ref)
                       if m.provider_token_id == hinted.provider_token_id), None)
    if listed is not None:
        truth = listed
    elif hinted.status in END_STATES:
        truth = hinted               # absence from the provider's list corroborates an end state
    else:
        raise ProviderUnavailable(f"token {hinted.provider_token_id} not listed by the provider yet; retrying")
    change = upsert_mandate(conn, tenant_id, truth, row.customer_id, now, raw_event_id=raw_event_id, clock=clock)
    return "changed" if change.changed else "unchanged"

