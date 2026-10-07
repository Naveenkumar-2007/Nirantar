"""Nirantar pay page (P8.6, ADR-0022): the customer-facing side of a payment request.

URL  {NIRANTAR_PUBLIC_APP_URL}/pay/{token}, token = tenant . request . keyed-hash(request) — unguessable, bound to one
     request, verifiable without a lookup table, and useless for any other request or business.
Shows only what the customer needs: business name, plan, amount, due date, their first name. No phone, no email.
Confirmation trusts nothing from the browser: the Razorpay signature must verify with the business's key secret, and
the payment itself is then fetched from the provider (amount, status) and applied through the same idempotent path
as webhooks — so a forged or replayed callback can never mark a debit paid.
"""

from __future__ import annotations

import dataclasses
import hmac
import os
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from nirantar.core.crypto import lookup_hash
from nirantar.db.session import tenant_tx
from nirantar.payments.domain import Capability, PaymentProvider
from nirantar.payments.processing import apply_payment


class PayError(ValueError):
    pass


def public_base() -> str | None:
    url = os.environ.get("NIRANTAR_PUBLIC_APP_URL", "").rstrip("/")
    return url or None


def supports_checkout(provider: PaymentProvider) -> bool:
    return Capability.CHECKOUT_ORDERS in provider.capabilities and public_base() is not None


def _sig(tenant_id: str, request_id: str) -> str:
    return lookup_hash(f"pay-page:{request_id}", tenant_id)[:32]


def token_for(tenant_id: str, request_id: str) -> str:
    return f"{tenant_id}.{request_id}.{_sig(tenant_id, request_id)}"


def parse_token(token: str) -> tuple[str, str]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].startswith("ten_") or not parts[1].startswith("prq_"):
        raise PayError("invalid payment link")
    tenant_id, request_id, sig = parts
    if not hmac.compare_digest(_sig(tenant_id, request_id), sig):
        raise PayError("invalid payment link")
    return tenant_id, request_id


def view(engine: Engine, token: str, checkout_key: str | None) -> dict[str, Any]:
    tenant_id, request_id = parse_token(token)
    with tenant_tx(tenant_id, engine) as c:
        r = c.execute(text(
            "SELECT r.status, r.kind, r.provider_link_id, r.amount_minor, "
            "coalesce(d.scheduled_for, i.due_on, (SELECT min(x.due_on) FROM billing.invoices x WHERE "
            "x.tenant_id=r.tenant_id AND x.invoice_id = ANY(r.invoice_ids)), CAST(r.created_at AS date)) "
            "AS scheduled_for, "
            "CASE WHEN d.status='succeeded' OR i.status='paid' OR k.status='paid' OR (r.invoice_ids IS NOT NULL AND "
            "NOT EXISTS (SELECT 1 "
            "FROM billing.invoices x WHERE x.tenant_id=r.tenant_id AND x.invoice_id = ANY(r.invoice_ids) AND "
            "x.status IN ('open','partially_paid'))) THEN 'succeeded' ELSE 'open' END AS debit_status, "
            "cu.display_name, cu.preferred_language, "
            "coalesce(p.name, 'Invoice ' || i.number, 'Statement: ' || cardinality(r.invoice_ids) || ' invoices', "
            "'Order ' || k.checkout_ref, "
            "'subscription') AS plan, t.name AS business "
            "FROM billing.payment_requests r "
            "LEFT JOIN billing.debits d ON d.tenant_id=r.tenant_id AND d.debit_id=r.debit_id "
            "LEFT JOIN billing.invoices i ON i.tenant_id=r.tenant_id AND i.invoice_id=r.invoice_id "
            "LEFT JOIN billing.checkout_sessions k ON k.tenant_id=r.tenant_id AND k.session_id=r.checkout_session_id "
            "JOIN billing.customers cu ON cu.tenant_id=r.tenant_id AND cu.customer_id=r.customer_id "
            "LEFT JOIN billing.subscriptions s ON s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id "
            "LEFT JOIN billing.plans p ON p.tenant_id=s.tenant_id AND p.plan_id=s.plan_id "
            "JOIN core.tenants t ON t.tenant_id=r.tenant_id WHERE r.tenant_id=:t AND r.request_id=:r"),
            {"t": tenant_id, "r": request_id}).one_or_none()
    if r is None or r.kind != "checkout":
        raise PayError("invalid payment link")
    paid = r.debit_status == "succeeded" or r.status == "paid"
    return {"business": r.business, "plan": r.plan, "amount_minor": int(r.amount_minor),
            "due": r.scheduled_for.isoformat(), "first_name": (r.display_name or "").split(" ")[0],
            "language": r.preferred_language, "status": "paid" if paid else ("closed" if r.status in
                                                                            ("expired", "cancelled") else "open"),
            "order_id": None if paid else r.provider_link_id, "checkout_key": None if paid else checkout_key}


def confirm(engine: Engine, provider: PaymentProvider, token: str, *, order_id: str, payment_id: str,
            signature: str, now: datetime) -> dict[str, Any]:
    tenant_id, request_id = parse_token(token)
    with tenant_tx(tenant_id, engine) as c:
        r = c.execute(text("SELECT debit_id, invoice_id, invoice_ids, checkout_session_id, provider_link_id, kind, "
                           "status FROM "
                           "billing.payment_requests WHERE tenant_id=:t AND request_id=:r"),
                      {"t": tenant_id, "r": request_id}).one_or_none()
    if r is None or r.kind != "checkout" or r.provider_link_id != order_id:
        raise PayError("this payment does not belong to this payment link")
    verify = getattr(provider, "verify_checkout", None)
    if verify is None or not verify(order_id, payment_id, signature):
        raise PayError("payment signature did not verify")
    p = provider.fetch_payment(payment_id)                      # provider truth, never the browser's word
    p = dataclasses.replace(p, notes={**dict(p.notes), "nirantar_ref": r.debit_id or r.invoice_id or request_id})
    with tenant_tx(tenant_id, engine) as c:
        outcome = apply_payment(c, tenant_id, provider, p, now, None, None)
        if p.status.value == "captured":
            c.execute(text("UPDATE billing.payment_requests SET status='paid', provider_payment_id=:pp, paid_at=:n "
                           "WHERE tenant_id=:t AND request_id=:r"),
                      {"pp": payment_id, "n": now, "t": tenant_id, "r": request_id})
        done: bool
        if r.checkout_session_id:                               # a checkout recovery: verified and booked
            done = c.execute(text("SELECT status FROM billing.checkout_sessions WHERE tenant_id=:t AND session_id=:s"),
                             {"t": tenant_id, "s": r.checkout_session_id}).scalar_one() == "paid"
        elif r.debit_id:
            done = c.execute(text("SELECT status FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                             {"t": tenant_id, "d": r.debit_id}).scalar_one() == "succeeded"
        else:                                                   # an invoice or a statement: the money was allocated
            ids = [r.invoice_id] if r.invoice_id else list(r.invoice_ids or [])
            done = bool(c.execute(text("SELECT count(*) FROM billing.payment_allocations WHERE tenant_id=:t AND "
                                       "provider_payment_id=:pp AND invoice_id = ANY(:ids)"),
                                  {"t": tenant_id, "pp": payment_id, "ids": ids}).scalar_one())
    return {"status": "paid" if done else p.status.value, "event": outcome.event_type}
