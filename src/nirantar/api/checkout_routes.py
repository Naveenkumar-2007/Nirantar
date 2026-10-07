"""Checkout drop-off recovery API (ADR-0028).

POST /v1/checkout/events          the business's server reports checkout events (scope checkout:ingest — a store key
                                  can do nothing else); idempotent on event_id
GET  /v1/checkout-recovery        funnel, causes and the recovery measured against the holdout
GET  /v1/checkout-sessions        recent checkouts with their recovery steps
GET  /v1/checkout-sessions/{id}   one checkout: events, steps, links
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.api.deps import Services, require, services, tenant_conn
from nirantar.checkout import service as checkouts
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.security.rbac import Permission, Principal

MAX_CLOCK_SKEW = timedelta(minutes=10)


class CheckoutCustomerIn(BaseModel):
    external_ref: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    phone_e164: str | None = Field(None, pattern=r"^\+[1-9][0-9]{7,14}$")
    email: str | None = Field(None, max_length=200)
    language: str = Field("en", pattern=r"^(en|hi|te|ta|kn|ml|mr|bn|gu|pa|or)$")
    consents: dict[str, bool] = Field(default_factory=dict)


class CheckoutItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    qty: int = Field(1, ge=1, le=999)


class CheckoutEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    type: Literal["initiated", "payment_page", "payment_failed", "paid", "expired"]
    checkout_ref: str = Field(min_length=1, max_length=120)
    at: datetime | None = None
    amount_rupees: Decimal | None = Field(None, gt=0, le=Decimal("10000000"))
    items: list[CheckoutItemIn] = Field(default_factory=list, max_length=50)
    customer: CheckoutCustomerIn | None = None
    failure_code: str | None = Field(None, max_length=80)
    provider: Literal["razorpay", "cashfree", "mock"] | None = None
    provider_order_id: str | None = Field(None, max_length=120)


def mount(app: FastAPI) -> None:
    @app.post("/v1/checkout/events")
    def checkout_event(body: CheckoutEventIn, p: Principal = Depends(require(Permission.CHECKOUT_INGEST)),
                       s: Services = Depends(services)) -> dict[str, Any]:
        now = datetime.now(UTC)
        at = body.at or now
        if at.tzinfo is None:
            raise HTTPException(422, "at must include a timezone")
        if at > now + MAX_CLOCK_SKEW:
            raise HTTPException(422, "event time is in the future")
        ev = checkouts.CheckoutEvent(
            event_id=body.event_id, type=body.type, checkout_ref=body.checkout_ref, at=at,
            amount=Money.of(str(body.amount_rupees.quantize(Decimal("0.01")))) if body.amount_rupees else None,
            items=[i.model_dump() for i in body.items],
            customer=checkouts.CheckoutCustomer(**body.customer.model_dump()) if body.customer else None,
            failure_code=body.failure_code, provider=body.provider, provider_order_id=body.provider_order_id)
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                return checkouts.record_event(c, p.tenant_id, ev, now)
        except checkouts.CheckoutError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/checkout-recovery")
    def checkout_recovery(days: int = Query(30, ge=1, le=365), c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return checkouts.stats(c, datetime.now(UTC), days)

    @app.get("/v1/checkout-sessions")
    def checkout_sessions(status: str | None = Query(None, pattern=r"^(open|paid|expired|stopped)$"),
                          limit: int = Query(100, ge=1, le=500),
                          c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        rows = [dict(r) for r in c.execute(text(
            "SELECT k.session_id, k.checkout_ref, k.customer_id, cu.display_name, k.amount_minor, k.currency, k.stage, "
            "k.status, k.attempts, k.cause, k.arm, k.paid_via, k.paid_minor, k.first_contact_at, k.created_at, "
            "k.last_activity_at, (SELECT step FROM ops.checkout_chases x WHERE x.tenant_id=k.tenant_id AND "
            "x.session_id=k.session_id ORDER BY created_at DESC LIMIT 1) AS last_step "
            "FROM billing.checkout_sessions k LEFT JOIN billing.customers cu ON cu.tenant_id=k.tenant_id AND "
            "cu.customer_id=k.customer_id WHERE (CAST(:s AS text) IS NULL OR k.status=:s) "
            "ORDER BY k.created_at DESC LIMIT :l"), {"s": status, "l": limit}).mappings()]
        for r in rows:
            for k in ("first_contact_at", "created_at", "last_activity_at"):
                r[k] = r[k].isoformat() if r[k] else None
        return {"items": rows}

    @app.get("/v1/checkout-sessions/{session_id}")
    def checkout_session(session_id: str, c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        k = c.execute(text("SELECT * FROM billing.checkout_sessions WHERE session_id=:s"),
                      {"s": session_id}).mappings().first()
        if k is None:
            raise HTTPException(404, "no such checkout")
        events = [dict(r) for r in c.execute(text(
            "SELECT event_id, type, at, payload FROM ops.checkout_events WHERE session_id=:s ORDER BY at"),
            {"s": session_id}).mappings()]
        steps = [dict(r) for r in c.execute(text(
            "SELECT step, status, action_id, detail, created_at FROM ops.checkout_chases WHERE session_id=:s "
            "ORDER BY created_at"), {"s": session_id}).mappings()]
        links = [dict(r) for r in c.execute(text(
            "SELECT request_id, url, amount_minor, status, created_at, paid_at FROM billing.payment_requests WHERE "
            "checkout_session_id=:s ORDER BY created_at"), {"s": session_id}).mappings()]
        return {"checkout": dict(k), "events": events, "steps": steps, "links": links}
