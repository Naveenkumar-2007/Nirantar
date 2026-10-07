"""Invoices: issue, collect (partially or fully, provider-verified), dispute, write off — and how old the money is.

Books: issuing an invoice accrues DR receivable / CR income (idempotent on the invoice). A payment counts only after
the provider confirms the capture for exactly that amount (`verify_capture`); then DR clearing / CR receivable, and
`paid_minor` grows. Partial payments are normal in B2B; the invoice is `paid` when nothing is outstanding.

The ladder (days relative to the due date): reminder −3 · due 0 · overdue_1 +3 · overdue_2 +7 · final +14 (always a
person's approval) · human +30 (handed to the team). It stops the moment an invoice is paid, disputed, written off
or cancelled.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.stores import Outbox, SqlLedgerStore
from nirantar.ledger import Ledger
from nirantar.ledger.ledger import Line
from nirantar.payments.domain import PaymentProvider, PaymentStatus, ProviderPayment
from nirantar.verifier.payments import INCOME, RECEIVABLE, clearing_account, ensure_accounts, verify_capture

LADDER: tuple[tuple[str, int], ...] = (("reminder", -3), ("due", 0), ("overdue_1", 3), ("overdue_2", 7),
                                       ("final", 14), ("human", 30))
OPEN = ("open", "partially_paid")
BUCKETS = (("not_due", None, -1), ("0_30", 0, 30), ("31_60", 31, 60), ("61_90", 61, 90), ("90_plus", 91, None))


class InvoiceError(ValueError):
    pass


def create_invoice(conn: Connection, tenant_id: str, *, customer_id: str, number: str, amount: Money,
                   issued_on: date, due_on: date, description: str | None, actor: str, now: datetime) -> str:
    number = number.strip()
    if not 1 <= len(number) <= 40:
        raise InvoiceError("invoice number must be 1-40 characters")
    if amount.currency != "INR" or not 100 <= amount.minor <= 1_000_000_000:          # ₹1 – ₹1 crore
        raise InvoiceError("amount must be between ₹1 and ₹1,00,00,000")
    if due_on < issued_on:
        raise InvoiceError("the due date cannot be before the issue date")
    if conn.execute(text("SELECT 1 FROM billing.customers WHERE customer_id=:c"), {"c": customer_id}).first() is None:
        raise InvoiceError("no such customer")
    if conn.execute(text("SELECT 1 FROM billing.invoices WHERE number=:n"), {"n": number}).first():
        raise InvoiceError(f"invoice {number} already exists")
    provider = conn.execute(text("SELECT provider FROM core.provider_accounts WHERE tenant_id=:t ORDER BY "
                                 "(mode='live') DESC, created_at DESC LIMIT 1"), {"t": tenant_id}).scalar_one_or_none()
    if provider is None:
        raise InvoiceError("connect a payment provider first")
    iid = new_id("ivc")
    conn.execute(text("INSERT INTO billing.invoices (tenant_id, invoice_id, customer_id, number, description, "
                      "issued_on, due_on, amount_minor, currency, status, created_by, created_at, updated_at) "
                      "VALUES (:t, :i, :c, :n, :d, :iss, :due, :a, 'INR', 'open', :by, :now, :now)"),
                 {"t": tenant_id, "i": iid, "c": customer_id, "n": number, "d": (description or "")[:300] or None,
                  "iss": issued_on, "due": due_on, "a": amount.minor, "by": actor, "now": now})
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, str(provider))
    ledger.post(tenant_id=tenant_id, idempotency_key=f"accrue:{iid}", memo=f"invoice issued {number} ({iid})",
                effective_at=now, lines=[Line(RECEIVABLE, debit=amount), Line(INCOME, credit=amount)])
    Outbox(conn).add(make_event(event_type="invoice.issued", version=1, tenant_id=tenant_id, subject_id=iid,
                                payload={"invoice_id": iid, "customer_id": customer_id, "due_on": due_on.isoformat(),
                                         "amount_minor": amount.minor},
                                source="receivables", occurred_at=now))
    return iid


def apply_payment(conn: Connection, tenant_id: str, provider: PaymentProvider, invoice_id: str, p: ProviderPayment,
                  now: datetime) -> dict[str, Any]:
    """A provider payment for one invoice (see `allocate`)."""
    return allocate(conn, tenant_id, provider, [invoice_id], p, now)


def allocate(conn: Connection, tenant_id: str, provider: PaymentProvider, invoice_ids: list[str], p: ProviderPayment,
             now: datetime) -> dict[str, Any]:
    """A provider payment for one invoice or a customer statement. Idempotent on the provider payment id; counts
    only when the provider confirms the capture for that exact amount. Verified money is booked once (DR clearing /
    CR receivable) and split across the invoices oldest-due-first; every split is recorded."""
    invs = conn.execute(text("SELECT * FROM billing.invoices WHERE invoice_id = ANY(:ids) ORDER BY due_on, issued_on, "
                             "number FOR UPDATE"), {"ids": invoice_ids}).all()
    if not invs:
        return {"applied": False, "reason": "no such invoice"}
    single = invoice_ids[0] if len(invoice_ids) == 1 else None
    inserted = conn.execute(text(
        "INSERT INTO billing.payments (tenant_id, payment_id, provider, provider_payment_id, customer_id, "
        "amount_minor, currency, status, method, issuer, error_code, error_reason, provider_created_at, invoice_id) "
        "VALUES (:t, :pid, :p, :pp, :c, :a, :cur, :s, :m, :iss, :ec, :er, :pc, :i) "
        "ON CONFLICT (tenant_id, provider, provider_payment_id) DO NOTHING RETURNING payment_id"),
        {"t": tenant_id, "pid": new_id("pmt"), "p": p.provider, "pp": p.provider_payment_id,
         "c": invs[0].customer_id, "a": p.amount.minor, "cur": p.amount.currency, "s": p.status.value,
         "m": p.method, "iss": p.issuer, "ec": p.error_code, "er": p.error_reason, "pc": p.created_at,
         "i": single}).first()
    if inserted is None or p.status != PaymentStatus.CAPTURED:
        return {"applied": False, "reason": "already applied" if inserted is None else p.status.value}
    v = verify_capture(provider, p.provider_payment_id, p.amount)
    if not v.verified:
        return {"applied": False, "reason": v.reason}
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, p.provider)
    numbers = ", ".join(i.number for i in invs)
    ledger.post(tenant_id=tenant_id, idempotency_key=f"settle:{p.provider}:{p.provider_payment_id}",
                memo=f"verified payment for invoice {numbers} ({', '.join(i.invoice_id for i in invs)})",
                effective_at=now, provider_ref=p.provider_payment_id,
                lines=[Line(clearing_account(p.provider), debit=p.amount), Line(RECEIVABLE, credit=p.amount)])
    left, splits = p.amount.minor, []
    for inv in invs:
        if left <= 0 or inv.status not in ("open", "partially_paid", "disputed"):
            continue
        owed = int(inv.amount_minor) - int(inv.paid_minor)
        take = min(left, owed)
        if take <= 0:
            continue
        left -= take
        paid = int(inv.paid_minor) + take
        status = "paid" if paid >= int(inv.amount_minor) else ("disputed" if inv.status == "disputed"
                                                                else "partially_paid")
        conn.execute(text("UPDATE billing.invoices SET paid_minor=:pd, status=:s, updated_at=:n, "
                          "closed_at=CASE WHEN :s='paid' THEN CAST(:n AS timestamptz) END WHERE invoice_id=:i"),
                     {"pd": paid, "s": status, "n": now, "i": inv.invoice_id})
        conn.execute(text("INSERT INTO billing.payment_allocations (tenant_id, provider, provider_payment_id, "
                          "invoice_id, amount_minor, created_at) VALUES (:t, :p, :pp, :i, :a, :n) "
                          "ON CONFLICT DO NOTHING"),
                     {"t": tenant_id, "p": p.provider, "pp": p.provider_payment_id, "i": inv.invoice_id, "a": take,
                      "n": now})
        Outbox(conn).add(make_event(event_type="invoice.payment_verified", version=1, tenant_id=tenant_id,
                                    subject_id=inv.invoice_id, payload={"invoice_id": inv.invoice_id,
                                                                         "amount_minor": take, "status": status},
                                    source="receivables", occurred_at=now))
        splits.append({"invoice_id": inv.invoice_id, "number": inv.number, "amount_minor": take, "status": status})
    applied = p.amount.minor - left
    return {"applied": True, "amount_minor": applied, "status": splits[-1]["status"] if splits else "unallocated",
            "allocations": splits, "overpaid_minor": left}


def attribute(p: ProviderPayment, invoice_id: str) -> ProviderPayment:
    return dataclasses.replace(p, notes={**dict(p.notes), "nirantar_ref": invoice_id})


def set_status(conn: Connection, invoice_id: str, status: str, now: datetime, reason: str | None = None) -> None:
    if status not in ("disputed", "written_off", "cancelled", "open"):
        raise InvoiceError("unsupported status change")
    row = conn.execute(text("SELECT status, paid_minor FROM billing.invoices WHERE invoice_id=:i"),
                       {"i": invoice_id}).one_or_none()
    if row is None:
        raise InvoiceError("no such invoice")
    if row.status in ("paid", "written_off", "cancelled"):
        raise InvoiceError(f"invoice is {row.status}")
    if status == "open":                                   # resolving a dispute
        status = "partially_paid" if int(row.paid_minor) else "open"
    conn.execute(text("UPDATE billing.invoices SET status=:s, dispute_reason=coalesce(:r, dispute_reason), "
                      "updated_at=:n, closed_at=CASE WHEN :s IN ('written_off','cancelled') "
                      "THEN CAST(:n AS timestamptz) END "
                      "WHERE invoice_id=:i"), {"s": status, "r": reason, "n": now, "i": invoice_id})


def ageing(conn: Connection, today: date) -> dict[str, Any]:
    rows = conn.execute(text("SELECT due_on, amount_minor - paid_minor AS owed FROM billing.invoices WHERE "
                             "status IN ('open','partially_paid')")).all()
    buckets = {name: {"n": 0, "minor": 0} for name, _, _ in BUCKETS}
    for r in rows:
        late = (today - r.due_on).days
        for name, lo, hi in BUCKETS:
            if (lo is None or late >= lo) and (hi is None or late <= hi):
                buckets[name]["n"] += 1
                buckets[name]["minor"] += int(r.owed)
                break
    paid = conn.execute(text("SELECT avg(closed_at::date - issued_on) AS days, count(*) AS n FROM billing.invoices "
                             "WHERE status='paid' AND closed_at >= :s"), {"s": today - timedelta(days=90)}).one()
    collected: int = conn.execute(text(
        "SELECT coalesce(sum(a.amount_minor), 0) FROM billing.payment_allocations a WHERE a.created_at >= :s"),
                             {"s": today - timedelta(days=30)}).scalar_one()
    return {"buckets": buckets, "outstanding_minor": sum(b["minor"] for b in buckets.values()),
            "days_to_pay_90d": round(float(paid.days), 1) if paid.days is not None else None,
            "paid_90d": int(paid.n), "collected_30d_minor": int(collected)}


def ladder_dates(due_on: date) -> list[tuple[str, date]]:
    return [(step, due_on + timedelta(days=offset)) for step, offset in LADDER]


def record_step(conn: Connection, tenant_id: str, invoice_id: str, step: str, status: str, action_id: str | None,
                detail: dict[str, Any], now: datetime) -> None:
    conn.execute(text("INSERT INTO ops.invoice_chases (tenant_id, invoice_id, step, status, action_id, detail, "
                      "created_at) VALUES (:t, :i, :s, :st, :a, CAST(:d AS jsonb), :n) ON CONFLICT (tenant_id, "
                      "invoice_id, step) DO UPDATE SET status=EXCLUDED.status, action_id=EXCLUDED.action_id, "
                      "detail=EXCLUDED.detail, created_at=EXCLUDED.created_at"),
                 {"t": tenant_id, "i": invoice_id, "s": step, "st": status, "a": action_id,
                  "d": json.dumps(detail, default=str), "n": now})
