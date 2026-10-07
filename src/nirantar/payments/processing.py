"""Process stored provider events into domain state.

Rules:
- The webhook body is a *hint*. For payments we re-fetch the provider record and
  act on that (ordering isn't guaranteed and bodies could be stale).
- State only moves forward (payments.state); regressions become discrepancies.
- Captures are settled in the ledger only after the Verifier confirms them.
- Everything for one event happens in one tenant transaction, together with the
  outbox events it produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.db.stores import Outbox
from nirantar.mandates import service as mandates
from nirantar.payments.domain import PaymentProvider, PaymentStatus, ProviderPayment, ProviderUnavailable
from nirantar.payments.repository import upsert_payment
from nirantar.verifier.payments import settle_offer_verified, settle_verified, verify_capture

MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class ProcessOutcome:
    status: str                  # processed | retry | dead | skipped
    event_type: str | None = None
    debit_id: str | None = None
    detail: str = ""


def find_debit(conn: Connection, tenant_id: str, p: ProviderPayment) -> tuple[str, str, int] | None:
    """Link a provider payment to a Nirantar debit: (debit_id, customer_id, amount_minor)."""
    ref = p.notes.get("nirantar_ref") or p.notes.get("reference_id")
    if ref:
        debit_id = ref.split(".", 1)[0]
        row = conn.execute(
            text("SELECT debit_id, customer_id, amount_minor FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
            {"t": tenant_id, "d": debit_id},
        ).one_or_none()
        if row:
            return row.debit_id, row.customer_id, row.amount_minor
    if p.order_ref:      # a Nirantar pay page: the order is recorded against its debit (notes may not be copied)
        row = conn.execute(
            text("SELECT d.debit_id, d.customer_id, d.amount_minor FROM billing.payment_requests r JOIN billing.debits "
                 "d ON d.tenant_id=r.tenant_id AND d.debit_id=r.debit_id WHERE r.tenant_id=:t AND r.provider=:p AND "
                 "r.kind='checkout' AND r.provider_link_id=:o"),
            {"t": tenant_id, "p": p.provider, "o": p.order_ref},
        ).one_or_none()
        if row:
            return row.debit_id, row.customer_id, row.amount_minor
    if p.subscription_ref:
        row = conn.execute(
            text(
                "SELECT d.debit_id, d.customer_id, d.amount_minor FROM billing.debits d "
                "JOIN billing.subscriptions s ON s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id "
                "WHERE d.tenant_id=:t AND s.provider=:p AND s.provider_subscription_id=:s "
                "AND d.status IN ('scheduled','notified','attempting','failed') "
                "ORDER BY d.scheduled_for DESC LIMIT 1"
            ),
            {"t": tenant_id, "p": p.provider, "s": p.subscription_ref},
        ).one_or_none()
        if row:
            return row.debit_id, row.customer_id, row.amount_minor
    return None


def find_offer(conn: Connection, tenant_id: str, p: ProviderPayment) -> tuple[str, str, int] | None:
    """Link a reactivation payment to its win-back offer: (offer_id, customer_id, offered amount)."""
    ref = p.notes.get("nirantar_ref") or p.notes.get("reference_id") or ""
    if not ref.startswith("ofr_"):
        return None
    row = conn.execute(text("SELECT offer_id, customer_id, offer_amount_minor FROM billing.offers WHERE tenant_id=:t "
                            "AND offer_id=:o"), {"t": tenant_id, "o": ref.split(".", 1)[0]}).one_or_none()
    return (row.offer_id, row.customer_id, row.offer_amount_minor) if row else None


def apply_dispute(conn: Connection, tenant_id: str, provider: PaymentProvider, provider_dispute_id: str,
                  now: datetime, raw_event_id: str | None, clock: Clock | None) -> ProcessOutcome:
    """Dispute webhook → re-fetch the dispute (provider truth) → upsert billing.disputes → dispute.opened/updated."""
    fetch = getattr(provider, "fetch_dispute", None)
    if fetch is None:
        raise ProviderUnavailable(f"{provider.name}: dispute API not supported by this adapter")
    d = fetch(provider_dispute_id)
    row0 = conn.execute(text("SELECT debit_id FROM billing.payments WHERE tenant_id=:t AND provider=:p AND "
                             "provider_payment_id=:pp"),
                        {"t": tenant_id, "p": d.provider, "pp": d.provider_payment_id}).one_or_none()
    if row0 is None:
        # the dispute overtook the payment's own webhook (or it was never delivered): import the payment from the
        # provider first, so the dispute is linked to its debit and the evidence is complete. Idempotent.
        debit_id = apply_payment(conn, tenant_id, provider, provider.fetch_payment(d.provider_payment_id), now,
                                 raw_event_id, clock).debit_id
    else:
        debit_id = row0.debit_id
    row = conn.execute(text(
        "INSERT INTO billing.disputes (tenant_id, dispute_id, provider, provider_dispute_id, provider_payment_id, "
        "debit_id, amount_minor, currency, reason_code, status, respond_by) VALUES (:t, :id, :p, :pd, :pp, :d, :a, "
        ":c, :r, :s, :rb) ON CONFLICT (tenant_id, provider, provider_dispute_id) DO UPDATE SET status = CASE "
        "WHEN billing.disputes.status IN ('evidence_ready','submitted') AND EXCLUDED.status='open' "
        "THEN billing.disputes.status ELSE EXCLUDED.status END, respond_by=EXCLUDED.respond_by, updated_at=now() "
        "RETURNING dispute_id, (xmax = 0) AS inserted, status"),
        {"t": tenant_id, "id": new_id("dsp"), "p": d.provider, "pd": d.provider_dispute_id, "pp": d.provider_payment_id,
         "d": debit_id, "a": d.amount.minor, "c": d.amount.currency, "r": d.reason_code, "s": d.status,
         "rb": d.respond_by or now + timedelta(days=7)}).one()
    event = "dispute.opened" if row.inserted else "dispute.updated"
    Outbox(conn).add(make_event(event_type=event, version=1, tenant_id=tenant_id, subject_id=row.dispute_id,
                                payload={"dispute_id": row.dispute_id, "status": row.status, "debit_id": debit_id,
                                         "amount_minor": d.amount.minor, "reason_code": d.reason_code,
                                         "respond_by": (d.respond_by or now).isoformat()},
                                source="payments/processing", occurred_at=now, causation_id=raw_event_id, clock=clock))
    return ProcessOutcome("processed", event, debit_id, row.dispute_id)


def find_invoices(conn: Connection, tenant_id: str, p: ProviderPayment) -> list[str]:
    """B2B receivables: the invoice(s) a payment is for — our reference on a link (an invoice, or a statement
    request), or the order recorded against an invoice / statement."""
    ref = (p.notes.get("nirantar_ref") or p.notes.get("reference_id") or "").split(".", 1)[0]
    if ref.startswith("ivc_"):
        return [ref]
    row = None
    if ref.startswith("prq_"):
        row = conn.execute(text("SELECT invoice_id, invoice_ids FROM billing.payment_requests WHERE tenant_id=:t AND "
                                "request_id=:r"), {"t": tenant_id, "r": ref}).first()
    elif p.order_ref:
        row = conn.execute(text(
            "SELECT invoice_id, invoice_ids FROM billing.payment_requests WHERE tenant_id=:t AND provider=:p AND "
            "kind='checkout' AND provider_link_id=:o"), {"t": tenant_id, "p": p.provider, "o": p.order_ref}).first()
    if row is None:
        return []
    return [row.invoice_id] if row.invoice_id else list(row.invoice_ids or [])


def apply_payment(conn: Connection, tenant_id: str, provider: PaymentProvider, p: ProviderPayment,
                  now: datetime, raw_event_id: str | None, clock: Clock | None) -> ProcessOutcome:
    invoice_ids = find_invoices(conn, tenant_id, p)
    if invoice_ids:                               # B2B receivables (ADR-0025): verified, partial-aware, ledgered
        from nirantar.receivables.service import allocate

        res = allocate(conn, tenant_id, provider, invoice_ids, p, now)
        return ProcessOutcome("processed", "invoice.payment_verified" if res["applied"] else None, None,
                              str(res.get("status") or res.get("reason") or ""))
    from nirantar.checkout import service as checkouts

    chk = checkouts.find_session(conn, tenant_id, p)
    if chk is not None:                           # checkout drop-off recovery (ADR-0028): verified, booked once
        res = checkouts.apply_provider_payment(conn, tenant_id, provider, chk[0], chk[1], p, now)
        return ProcessOutcome("processed", "checkout.updated" if res["applied"] else None, None,
                              str(res.get("status") or res.get("reason") or ""))
    link = find_debit(conn, tenant_id, p)
    offer = None if link else find_offer(conn, tenant_id, p)
    debit_id, customer_id, due_minor = link if link else (None, None, None)
    if offer is not None:
        customer_id, due_minor = offer[1], offer[2]       # verified against the OFFERED amount
    if customer_id is None and p.subscription_ref:
        # a payment outside any debit/offer (e.g. a churned subscriber resuming on their own) still belongs to a
        # customer — without this, spontaneous reactivations are invisible and the win-back holdout looks worse
        customer_id = conn.execute(text("SELECT customer_id FROM billing.subscriptions WHERE tenant_id=:t AND "
                                        "provider=:p AND provider_subscription_id=:s"),
                                   {"t": tenant_id, "p": p.provider, "s": p.subscription_ref}).scalar_one_or_none()
    if customer_id is None and p.customer_ref and p.token_ref:
        # e.g. a mandate (re-)registration payment: the provider customer is known from an earlier mandate
        customer_id = conn.execute(text("SELECT customer_id FROM billing.mandates WHERE tenant_id=:t AND provider=:p "
                                        "AND provider_customer_ref=:c ORDER BY created_at LIMIT 1"),
                                   {"t": tenant_id, "p": p.provider, "c": p.customer_ref}).scalar_one_or_none()
    status, changed = upsert_payment(conn, tenant_id, p, debit_id=debit_id, customer_id=customer_id,
                                     raw_event_id=raw_event_id)
    if status in (PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED, PaymentStatus.FAILED):
        mandates.discover_from_payment(conn, tenant_id, provider, p, customer_id, now, raw_event_id, clock)
    if not changed:
        return ProcessOutcome("processed", None, debit_id, "no_state_change")
    outbox = Outbox(conn)
    subject = debit_id or (offer[0] if offer else None) or p.provider_payment_id
    base: dict[str, Any] = {"provider": p.provider, "provider_payment_id": p.provider_payment_id,
                            "debit_id": debit_id, "customer_id": customer_id, "amount_minor": p.amount.minor,
                            "currency": p.amount.currency}
    if status == PaymentStatus.CAPTURED:
        verification = verify_capture(provider, p.provider_payment_id,
                                      Money(due_minor) if due_minor is not None else p.amount)
        payload = {**base, "verified": verification.verified, "verification_reason": verification.reason,
                   "evidence_hash": verification.evidence_hash}
        if verification.verified and offer is not None:
            payload["ledger_entry_id"] = settle_offer_verified(conn, tenant_id, offer[0], verification, p, now)
            payload["offer_id"] = offer[0]
            conn.execute(text("UPDATE billing.offers SET status='redeemed', redeemed_payment_id=:pp WHERE "
                              "tenant_id=:t AND offer_id=:o AND status IN ('created','sent')"),
                         {"pp": p.provider_payment_id, "t": tenant_id, "o": offer[0]})
        if verification.verified and debit_id:
            entry_id = settle_verified(conn, tenant_id, debit_id, verification, p, now)
            conn.execute(
                text("UPDATE billing.debits SET status='succeeded', provider_payment_id=:pp, updated_at=now() "
                     "WHERE tenant_id=:t AND debit_id=:d AND status <> 'succeeded'"),
                {"pp": p.provider_payment_id, "t": tenant_id, "d": debit_id},
            )
            payload["ledger_entry_id"] = entry_id
        outbox.add(make_event(event_type="payment.captured", version=1, tenant_id=tenant_id, subject_id=subject,
                              payload=payload, source="payments/processing", occurred_at=now,
                              causation_id=raw_event_id, clock=clock))
        return ProcessOutcome("processed", "payment.captured", debit_id,
                              "settled" if verification.verified and debit_id else verification.reason)
    if status == PaymentStatus.FAILED:
        if debit_id:
            conn.execute(
                text("UPDATE billing.debits SET status='failed', last_error_code=:e, attempt_count=attempt_count+1,"
                     " updated_at=now() WHERE tenant_id=:t AND debit_id=:d AND status NOT IN ('succeeded')"),
                {"e": p.error_code, "t": tenant_id, "d": debit_id},
            )
        outbox.add(make_event(event_type="payment.failed", version=1, tenant_id=tenant_id, subject_id=subject,
                              payload={**base, "error_code": p.error_code, "error_reason": p.error_reason},
                              source="payments/processing", occurred_at=now, causation_id=raw_event_id,
                              clock=clock))
        return ProcessOutcome("processed", "payment.failed", debit_id)
    outbox.add(make_event(event_type=f"payment.{status.value}", version=1, tenant_id=tenant_id, subject_id=subject,
                          payload=base, source="payments/processing", occurred_at=now, causation_id=raw_event_id,
                          clock=clock))
    return ProcessOutcome("processed", f"payment.{status.value}", debit_id)


def process_raw_event(engine: Engine, provider: PaymentProvider, tenant_id: str, raw_event_id: str,
                      clock: Clock | None = None) -> ProcessOutcome:
    now = (clock or SystemClock()).now()
    with tenant_tx(tenant_id, engine) as c:
        row = c.execute(
            text("SELECT status, attempts, body FROM ingest.provider_events WHERE tenant_id=:t AND raw_event_id=:r "
                 "FOR UPDATE"),
            {"t": tenant_id, "r": raw_event_id},
        ).one_or_none()
        if row is None:
            return ProcessOutcome("skipped", detail="not_found")
        if row.status in ("processed", "dead", "rejected"):
            return ProcessOutcome("skipped", detail=f"already_{row.status}")
        normalized = provider.parse_webhook({}, bytes(row.body))
        try:
            payment_entity = normalized.entities.get("payment")
            dispute_entity = normalized.entities.get("dispute")
            token_entity = normalized.entities.get("token")
            mapper = getattr(provider, "mandate_from_webhook", None)
            if (normalized.event_type.startswith("mandate.") and isinstance(token_entity, dict)
                    and "id" in token_entity and mapper is not None):
                result = mandates.apply_token_event(c, tenant_id, provider, mapper(token_entity), now, raw_event_id,
                                                    clock)
                outcome = ProcessOutcome("processed", "mandate.updated" if result == "changed" else None,
                                         detail=result)
            elif (normalized.event_type.startswith("dispute.") and isinstance(dispute_entity, dict)
                    and "id" in dispute_entity):
                outcome = apply_dispute(c, tenant_id, provider, str(dispute_entity["id"]), now, raw_event_id, clock)
            elif isinstance(payment_entity, dict) and "id" in payment_entity:
                fresh = provider.fetch_payment(str(payment_entity["id"]))  # provider truth, not the body
                outcome = apply_payment(c, tenant_id, provider, fresh, now, raw_event_id, clock)
            else:
                Outbox(c).add(make_event(event_type=normalized.event_type, version=1, tenant_id=tenant_id,
                                         subject_id=normalized.subject_ref,
                                         payload={"provider": provider.name, "raw_event_id": raw_event_id,
                                                  "provider_event_type": normalized.provider_event_type},
                                         source="payments/processing", occurred_at=now,
                                         causation_id=raw_event_id, clock=clock))
                outcome = ProcessOutcome("processed", normalized.event_type)
        except ProviderUnavailable as exc:
            attempts = row.attempts + 1
            status = "dead" if attempts >= MAX_ATTEMPTS else "received"
            c.execute(
                text("UPDATE ingest.provider_events SET attempts=:a, status=:s, last_error=:e "
                     "WHERE tenant_id=:t AND raw_event_id=:r"),
                {"a": attempts, "s": status, "e": str(exc)[:500], "t": tenant_id, "r": raw_event_id},
            )
            return ProcessOutcome("dead" if status == "dead" else "retry", detail=str(exc))
        c.execute(
            text("UPDATE ingest.provider_events SET status='processed', attempts=attempts+1 "
                 "WHERE tenant_id=:t AND raw_event_id=:r"),
            {"t": tenant_id, "r": raw_event_id},
        )
        return outcome
