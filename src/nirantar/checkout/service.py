"""Checkout drop-off recovery (ADR-0028): what the business's checkout reports, what the provider confirms, and the
measured result.

Events come from two sources and are recorded once each (idempotent on event id):
  * the business's server (POST /v1/checkout/events): initiated · payment_page · payment_failed · paid · expired
  * the payment provider: a failed or captured payment on the checkout's own order, or on a Nirantar recovery link
Money is only counted when the provider confirms it: a recovery-link payment is verified with the provider, booked
once (DR clearing / CR income) and marks the checkout paid via the recovery link. A business-reported "paid" closes the
checkout but is never booked — it is the business's money flow, not ours.
Every checkout is assigned to treatment or holdout (per customer, the tenant's holdout share), so the recovery rate is
always reported against what would have happened anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.agents.failure_triage import RULES
from nirantar.billing.service import NewCustomer, create_customer
from nirantar.contracts.events import make_event
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.stores import Outbox, SqlLedgerStore
from nirantar.experiments import service as experiments
from nirantar.ledger import Ledger
from nirantar.ledger.ledger import Line
from nirantar.payments.domain import PaymentProvider, PaymentStatus, ProviderPayment
from nirantar.verifier.payments import INCOME, clearing_account, ensure_accounts, verify_capture

EVENT_TYPES = ("initiated", "payment_page", "payment_failed", "paid", "expired")
EXPERIMENT = "checkout-recovery"
MAX_ITEMS = 10
CANCELLED = frozenset({"payment_cancelled", "user_cancelled", "cancelled_by_user", "payment_cancelled_by_user"})


class CheckoutError(ValueError):
    pass


@dataclass(frozen=True)
class CheckoutCustomer:
    external_ref: str
    name: str
    phone_e164: str | None = None
    email: str | None = None
    language: str = "en"
    consents: dict[str, Any] | None = None


@dataclass(frozen=True)
class CheckoutEvent:
    event_id: str
    type: str
    checkout_ref: str
    at: datetime
    amount: Money | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    customer: CheckoutCustomer | None = None
    failure_code: str | None = None
    provider: str | None = None
    provider_order_id: str | None = None


def _customer_id(conn: Connection, tenant_id: str, c: CheckoutCustomer) -> str:
    found: str | None = conn.execute(text("SELECT customer_id FROM billing.customers WHERE tenant_id=:t AND "
                                          "external_ref=:r"),
                                     {"t": tenant_id, "r": c.external_ref}).scalar_one_or_none()
    if found:
        return found
    return create_customer(conn, tenant_id, NewCustomer(c.external_ref, c.name, c.phone_e164, c.email, c.language,
                                                         segment="ecommerce", consents=c.consents or {}))


def _items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only what a reminder needs (name, quantity); never prices from the cart — the amount is the checkout's own."""
    out = []
    for it in items[:MAX_ITEMS]:
        name = str(it.get("name", ""))[:80].strip()
        if name:
            out.append({"name": name, "qty": max(1, min(int(it.get("qty", 1) or 1), 999))})
    return out


def record_event(conn: Connection, tenant_id: str, ev: CheckoutEvent, now: datetime) -> dict[str, Any]:
    """Apply one checkout event. Returns {"session_id", "duplicate", "new", "status"}. Idempotent on event_id; events
    after a checkout closed are recorded but change nothing."""
    if ev.type not in EVENT_TYPES:
        raise CheckoutError(f"unknown checkout event type {ev.type!r}")
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                 {"k": f"checkout:{tenant_id}:{ev.checkout_ref}"})
    row = conn.execute(text("SELECT * FROM billing.checkout_sessions WHERE tenant_id=:t AND checkout_ref=:r"),
                       {"t": tenant_id, "r": ev.checkout_ref}).first()
    seen = conn.execute(text("SELECT session_id FROM ops.checkout_events WHERE tenant_id=:t AND event_id=:e"),
                        {"t": tenant_id, "e": ev.event_id}).scalar_one_or_none()
    if seen is not None:
        return {"session_id": seen, "duplicate": True, "new": False, "status": row.status if row else None}
    new = row is None
    if new:
        if ev.amount is None or ev.amount.minor <= 0:
            raise CheckoutError("the first event for a checkout must carry its amount")
        customer_id = _customer_id(conn, tenant_id, ev.customer) if ev.customer else None
        session_id = new_id("chk")
        conn.execute(text(
            "INSERT INTO billing.checkout_sessions (tenant_id, session_id, checkout_ref, customer_id, amount_minor, "
            "currency, items, stage, status, provider, provider_order_id, created_at, last_activity_at) VALUES "
            "(:t, :s, :r, :c, :a, :cur, CAST(:it AS jsonb), 'initiated', 'open', :p, :o, :at, :at)"),
            {"t": tenant_id, "s": session_id, "r": ev.checkout_ref, "c": customer_id, "a": ev.amount.minor,
             "cur": ev.amount.currency, "it": json.dumps(_items(ev.items)), "p": ev.provider,
             "o": ev.provider_order_id, "at": ev.at})
        status = "open"
    else:
        assert row is not None
        session_id, status = row.session_id, row.status
        if ev.customer and row.customer_id is None:
            conn.execute(text("UPDATE billing.checkout_sessions SET customer_id=:c WHERE tenant_id=:t AND "
                              "session_id=:s"), {"c": _customer_id(conn, tenant_id, ev.customer), "t": tenant_id,
                                                 "s": session_id})
        if ev.provider_order_id and row.provider_order_id is None:
            conn.execute(text("UPDATE billing.checkout_sessions SET provider=:p, provider_order_id=:o WHERE "
                              "tenant_id=:t AND session_id=:s"),
                         {"p": ev.provider, "o": ev.provider_order_id, "t": tenant_id, "s": session_id})
    conn.execute(text("INSERT INTO ops.checkout_events (tenant_id, event_id, session_id, type, at, payload) VALUES "
                      "(:t, :e, :s, :ty, :at, CAST(:p AS jsonb))"),
                 {"t": tenant_id, "e": ev.event_id, "s": session_id, "ty": ev.type, "at": ev.at,
                  "p": json.dumps({"failure_code": ev.failure_code, "amount_minor": ev.amount.minor if ev.amount
                                   else None})})
    if status == "open":
        status = _transition(conn, tenant_id, session_id, ev)
    Outbox(conn).add(make_event(event_type="checkout.updated", version=1, tenant_id=tenant_id, subject_id=session_id,
                                payload={"session_id": session_id, "type": ev.type, "new": new, "status": status},
                                source="checkout", occurred_at=now))
    return {"session_id": session_id, "duplicate": False, "new": new, "status": status}


def _transition(conn: Connection, tenant_id: str, session_id: str, ev: CheckoutEvent) -> str:
    sets, status = ["last_activity_at = GREATEST(last_activity_at, :at)", "updated_at = now()"], "open"
    params: dict[str, Any] = {"t": tenant_id, "s": session_id, "at": ev.at}
    if ev.type == "payment_page":
        sets.append("stage = 'payment_page'")
    elif ev.type == "payment_failed":
        sets += ["stage = 'payment_page'", "attempts = attempts + 1", "last_failure_code = :fc"]
        params["fc"] = (ev.failure_code or "unknown")[:80]
    elif ev.type == "paid":
        status = "paid"
        sets += ["status = 'paid'", "paid_at = :at", "paid_via = 'original'",
                 "paid_minor = coalesce(:pm, amount_minor)"]
        params["pm"] = ev.amount.minor if ev.amount else None
    elif ev.type == "expired":
        status = "expired"
        sets += ["status = 'expired'", "stop_reason = 'expired by the business'"]
    conn.execute(text(f"UPDATE billing.checkout_sessions SET {', '.join(sets)} WHERE tenant_id=:t AND "  # noqa: S608
                      "session_id=:s AND status='open'"), params)
    return status


def diagnose(attempts: int, last_failure_code: str | None, stage: str) -> str:
    """Why the checkout did not complete — decides the wording, never the amount."""
    if attempts >= 3:
        return "repeated_failures"
    if attempts > 0:
        code = (last_failure_code or "").strip().lower()
        if code in CANCELLED:
            return "customer_cancelled"
        category = RULES.get(code)
        return {"BANK_TECHNICAL": "bank_issue", "INSUFFICIENT_FUNDS": "insufficient_funds",
                "CARD_EXPIRED": "card_problem", "LIMIT_EXCEEDED": "limit_exceeded"}.get(category or "",
                                                                                      "payment_failed")
    return "abandoned_at_payment" if stage == "payment_page" else "abandoned_cart"


def assess(conn: Connection, tenant_id: str, session_id: str, now: datetime, min_amount_minor: int) -> dict[str, Any]:
    """Diagnose, decide eligibility and assign the experiment arm — once per checkout (later calls return the same)."""
    s = conn.execute(text("SELECT * FROM billing.checkout_sessions WHERE tenant_id=:t AND session_id=:s FOR UPDATE"),
                     {"t": tenant_id, "s": session_id}).one()
    cause = s.cause or diagnose(int(s.attempts), s.last_failure_code, s.stage)
    reason = None
    if s.status != "open":
        reason = f"checkout is {s.status}"
    elif s.customer_id is None:
        reason = "no customer to contact"
    elif int(s.amount_minor) < min_amount_minor:
        reason = f"below the recovery minimum ({min_amount_minor / 100:,.0f} rupees)"
    elif cause == "customer_cancelled" and int(s.attempts) == 1:
        reason = None                                 # a single cancel is still worth one gentle reminder
    arm = s.arm
    if reason is None and arm is None:
        exp = experiments.ensure_experiment(conn, tenant_id, EXPERIMENT)
        arm = "holdout" if experiments.assign(conn, tenant_id, exp, s.customer_id, now) == experiments.HOLDOUT \
            else "treatment"
        conn.execute(text("UPDATE billing.checkout_sessions SET arm=:a, experiment_id=:e, cause=:c, updated_at=now() "
                          "WHERE tenant_id=:t AND session_id=:s"),
                     {"a": arm, "e": exp, "c": cause, "t": tenant_id, "s": session_id})
    elif s.cause is None:
        conn.execute(text("UPDATE billing.checkout_sessions SET cause=:c WHERE tenant_id=:t AND session_id=:s"),
                     {"c": cause, "t": tenant_id, "s": session_id})
    return {"eligible": reason is None, "reason": reason, "cause": cause, "arm": arm,
            "customer_id": s.customer_id, "amount_minor": int(s.amount_minor)}


def close(conn: Connection, tenant_id: str, session_id: str, status: str, reason: str, now: datetime) -> None:
    conn.execute(text("UPDATE billing.checkout_sessions SET status=:st, stop_reason=:r, updated_at=:n WHERE "
                      "tenant_id=:t AND session_id=:s AND status='open'"),
                 {"st": status, "r": reason, "n": now, "t": tenant_id, "s": session_id})


def record_step(conn: Connection, tenant_id: str, session_id: str, step: str, status: str, action_id: str | None,
                detail: dict[str, Any], now: datetime) -> None:
    conn.execute(text("INSERT INTO ops.checkout_chases (tenant_id, session_id, step, status, action_id, detail, "
                      "created_at) VALUES (:t, :s, :st, :ss, :a, CAST(:d AS jsonb), :n) ON CONFLICT (tenant_id, "
                      "session_id, step) DO UPDATE SET status=EXCLUDED.status, action_id=EXCLUDED.action_id, "
                      "detail=EXCLUDED.detail, created_at=EXCLUDED.created_at"),
                 {"t": tenant_id, "s": session_id, "st": step, "ss": status, "a": action_id,
                  "d": json.dumps(detail, default=str), "n": now})


# ---------------------------------------------------------------- provider truth
def find_session(conn: Connection, tenant_id: str, p: ProviderPayment) -> tuple[str, str] | None:
    """(session_id, how) for a provider payment on a checkout: our recovery link/page, or the checkout's own order."""
    ref = (p.notes.get("nirantar_ref") or p.notes.get("reference_id") or "").split(".", 1)[0]
    row = None
    if ref.startswith("prq_"):
        row = conn.execute(text("SELECT checkout_session_id FROM billing.payment_requests WHERE tenant_id=:t AND "
                                "request_id=:r AND checkout_session_id IS NOT NULL"),
                           {"t": tenant_id, "r": ref}).first()
    elif p.order_ref:
        row = conn.execute(text("SELECT checkout_session_id FROM billing.payment_requests WHERE tenant_id=:t AND "
                                "provider=:p AND kind='checkout' AND provider_link_id=:o AND "
                                "checkout_session_id IS NOT NULL"),
                           {"t": tenant_id, "p": p.provider, "o": p.order_ref}).first()
    if row is not None:
        return str(row[0]), "recovery_link"
    if p.order_ref:
        own = conn.execute(text("SELECT session_id FROM billing.checkout_sessions WHERE tenant_id=:t AND provider=:p "
                                "AND provider_order_id=:o"),
                           {"t": tenant_id, "p": p.provider, "o": p.order_ref}).scalar_one_or_none()
        if own:
            return str(own), "original"
    return None


def apply_provider_payment(conn: Connection, tenant_id: str, provider: PaymentProvider, session_id: str, how: str,
                           p: ProviderPayment, now: datetime) -> dict[str, Any]:
    """A provider payment on a checkout. Failures on the checkout's own order are recorded as failed attempts.
    A capture through a recovery link is verified, booked once and closes the checkout as recovered; a capture on
    the original order closes it as paid (the business books its own sale)."""
    s = conn.execute(text("SELECT * FROM billing.checkout_sessions WHERE tenant_id=:t AND session_id=:s FOR UPDATE"),
                     {"t": tenant_id, "s": session_id}).one()
    inserted = conn.execute(text(
        "INSERT INTO billing.payments (tenant_id, payment_id, provider, provider_payment_id, customer_id, "
        "amount_minor, currency, status, method, issuer, error_code, error_reason, provider_created_at, "
        "checkout_session_id) VALUES "
        "(:t, :pid, :p, :pp, :c, :a, :cur, :st, :m, :iss, :ec, :er, :pc, :s) ON CONFLICT (tenant_id, provider, "
        "provider_payment_id) DO UPDATE SET status = EXCLUDED.status WHERE billing.payments.status <> EXCLUDED.status "
        "RETURNING payment_id"),
        {"t": tenant_id, "pid": new_id("pmt"), "p": p.provider, "pp": p.provider_payment_id, "c": s.customer_id,
         "a": p.amount.minor, "cur": p.amount.currency, "st": p.status.value, "m": p.method, "iss": p.issuer,
         "ec": p.error_code, "er": p.error_reason, "pc": p.created_at, "s": session_id}).first()
    if inserted is None:
        return {"applied": False, "reason": "already applied"}
    if p.status == PaymentStatus.FAILED:
        if how == "original":
            record_event(conn, tenant_id, CheckoutEvent(f"provider:{p.provider}:{p.provider_payment_id}",
                                                        "payment_failed", s.checkout_ref, p.created_at or now,
                                                        failure_code=p.error_reason or p.error_code), now)
        return {"applied": True, "status": "failed_attempt"}
    if p.status != PaymentStatus.CAPTURED:
        return {"applied": False, "reason": p.status.value}
    if how == "original":
        record_event(conn, tenant_id, CheckoutEvent(f"provider:{p.provider}:{p.provider_payment_id}", "paid",
                                                    s.checkout_ref, p.created_at or now, amount=p.amount), now)
        return {"applied": True, "status": "paid"}
    v = verify_capture(provider, p.provider_payment_id, Money(int(s.amount_minor), s.currency))
    if not v.verified:
        return {"applied": False, "reason": v.reason}
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, p.provider)
    entry = ledger.post(tenant_id=tenant_id, idempotency_key=f"settle:{p.provider}:{p.provider_payment_id}",
                        memo=f"verified recovered checkout {s.checkout_ref} ({session_id})", effective_at=now,
                        provider_ref=p.provider_payment_id,
                        lines=[Line(clearing_account(p.provider), debit=p.amount), Line(INCOME, credit=p.amount)])
    conn.execute(text("UPDATE billing.checkout_sessions SET status='paid', paid_at=:n, paid_minor=:a, "
                      "paid_via='recovery_link', last_activity_at=:n, updated_at=:n WHERE tenant_id=:t AND "
                      "session_id=:s AND status='open'"),
                 {"n": now, "a": p.amount.minor, "t": tenant_id, "s": session_id})
    conn.execute(text("UPDATE billing.payment_requests SET status='paid', provider_payment_id=:pp, paid_at=:n WHERE "
                      "tenant_id=:t AND checkout_session_id=:s AND status IN ('created','sent')"),
                 {"pp": p.provider_payment_id, "n": now, "t": tenant_id, "s": session_id})
    Outbox(conn).add(make_event(event_type="checkout.updated", version=1, tenant_id=tenant_id, subject_id=session_id,
                                payload={"session_id": session_id, "type": "paid", "new": False, "status": "paid",
                                         "verified": True}, source="checkout", occurred_at=now))
    return {"applied": True, "status": "recovered", "ledger_entry_id": getattr(entry, "entry_id", None)}


# ---------------------------------------------------------------- measurement
def stats(conn: Connection, now: datetime, days: int = 30) -> dict[str, Any]:
    """The checkout funnel and recovery measured against the holdout, for the last `days` days."""
    since = now - timedelta(days=days)
    f = conn.execute(text(
        "SELECT count(*) AS checkouts, "
        "count(*) FILTER (WHERE status='paid' AND paid_via='original' AND first_contact_at IS NULL) AS paid_unaided, "
        "count(*) FILTER (WHERE stage='payment_page') AS reached_payment, "
        "count(*) FILTER (WHERE attempts > 0) AS had_failure, "
        "count(*) FILTER (WHERE cause IS NOT NULL) AS dropped_off, "
        "count(*) FILTER (WHERE first_contact_at IS NOT NULL) AS contacted, "
        "count(*) FILTER (WHERE status='paid' AND first_contact_at IS NOT NULL) AS recovered, "
        "coalesce(sum(paid_minor) FILTER (WHERE status='paid' AND paid_via='recovery_link'), 0) AS verified_minor, "
        "coalesce(sum(amount_minor) FILTER (WHERE status='open'), 0) AS open_minor "
        "FROM billing.checkout_sessions WHERE created_at >= :since"), {"since": since}).one()
    per: dict[str, list[tuple[bool, int]]] = {}
    for r in conn.execute(text("SELECT arm, status, coalesce(paid_minor, 0) AS v FROM billing.checkout_sessions "
                               "WHERE created_at >= :since AND arm IS NOT NULL AND status <> 'open'"),
                          {"since": since}):
        arm = experiments.HOLDOUT if r.arm == "holdout" else "treatment"
        per.setdefault(arm, []).append((r.status == "paid", int(r.v) if r.status == "paid" else 0))
    causes = {r.cause: int(r.n) for r in conn.execute(text(
        "SELECT cause, count(*) AS n FROM billing.checkout_sessions WHERE created_at >= :since AND cause IS NOT NULL "
        "GROUP BY cause ORDER BY n DESC"), {"since": since})}
    return {"window_days": days, "funnel": {k: int(v) for k, v in f._mapping.items()}, "causes": causes,
            "experiment": experiments.incremental(per)}
