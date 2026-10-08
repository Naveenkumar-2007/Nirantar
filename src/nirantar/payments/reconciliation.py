"""Reconciliation poller: provider state is the source of truth; webhooks are an optimisation.

Heals missed/disabled webhooks (Razorpay disables after 24h of failures; Cashfree
can't replay subscription webhooks) by pulling provider payments and applying
the same idempotent `apply_payment` path that webhooks use.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from nirantar.core.clock import Clock, SystemClock
from nirantar.db.session import tenant_tx
from nirantar.payments.domain import Capability, PaymentProvider, ProviderError
from nirantar.payments.processing import apply_payment


@dataclass
class ReconReport:
    scanned: int = 0
    changed: int = 0
    events: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def reconcile_window(engine: Engine, provider: PaymentProvider, tenant_id: str, since: datetime,
                     until: datetime, clock: Clock | None = None) -> ReconReport:
    report = ReconReport()
    now = (clock or SystemClock()).now()
    if Capability.SUBSCRIPTIONS in provider.capabilities and provider.name == "cashfree":
        payments_iter = []  # cashfree reconciles per subscription (see reconcile_open_debits)
    else:
        try:
            payments_iter = list(provider.list_payments(since, until))
        except ProviderError as exc:
            report.errors.append(str(exc))
            return report
    for p in payments_iter:
        report.scanned += 1
        with tenant_tx(tenant_id, engine) as c:
            outcome = apply_payment(c, tenant_id, provider, p, now, None, clock)
        if outcome.event_type:
            report.changed += 1
            report.events.append(outcome.event_type)
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("UPDATE core.provider_accounts SET last_reconciled_at=:n WHERE tenant_id=:t AND provider=:p"),
                  {"n": now, "t": tenant_id, "p": provider.name})
    return report


def reconcile_mandate_attempts(engine: Engine, provider: PaymentProvider, tenant_id: str,
                               debit_id: str | None = None, clock: Clock | None = None) -> ReconReport:
    """Nirantar's own mandate charges still 'charging' (no webhook yet): ask the provider for the order's payments
    (or the payment itself) and apply them through the same verified path as a webhook (ADR-0029)."""
    report = ReconReport()
    now = (clock or SystemClock()).now()
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text(
            "SELECT debit_id, attempt, provider_order_id, provider_payment_id FROM billing.debit_attempts WHERE "
            "tenant_id=:t AND status='charging' AND (CAST(:d AS text) IS NULL OR debit_id=:d)"),
            {"t": tenant_id, "d": debit_id}).all()
    fetch_order = getattr(provider, "fetch_order_payments", None)
    for r in rows:
        try:
            if r.provider_order_id and fetch_order is not None:
                payments = fetch_order(r.provider_order_id)
            elif r.provider_payment_id:
                payments = [provider.fetch_payment(r.provider_payment_id)]
            else:
                continue
        except ProviderError as exc:
            report.errors.append(f"{r.debit_id}#{r.attempt}: {exc}")
            continue
        for p in payments:
            report.scanned += 1
            with tenant_tx(tenant_id, engine) as c:
                outcome = apply_payment(c, tenant_id, provider, p, now, None, clock)
            if outcome.event_type:
                report.changed += 1
                report.events.append(outcome.event_type)
    return report


def reconcile_open_debits(engine: Engine, provider: PaymentProvider, tenant_id: str, stale_after: timedelta,
                          clock: Clock | None = None) -> ReconReport:
    """Debits stuck in 'attempting' with no webhook: pull their subscription's payments directly."""
    report = ReconReport()
    now = (clock or SystemClock()).now()
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(
            text(
                "SELECT DISTINCT s.provider_subscription_id FROM billing.debits d JOIN billing.subscriptions s "
                "ON s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id "
                "WHERE d.tenant_id=:t AND s.provider=:p AND d.status='attempting' AND d.updated_at < :cut "
                "AND s.provider_subscription_id IS NOT NULL"
            ),
            {"t": tenant_id, "p": provider.name, "cut": now - stale_after},
        ).all()
    for r in rows:
        try:
            payments = provider.list_subscription_payments(r.provider_subscription_id)
        except ProviderError as exc:
            report.errors.append(f"{r.provider_subscription_id}: {exc}")
            continue
        for p in payments:
            report.scanned += 1
            with tenant_tx(tenant_id, engine) as c:
                outcome = apply_payment(c, tenant_id, provider, p, now, None, clock)
            if outcome.event_type:
                report.changed += 1
                report.events.append(outcome.event_type)
    return report


def reconcile_payment_requests(engine: Engine, provider: PaymentProvider, tenant_id: str,
                               debit_id: str | None = None, clock: Clock | None = None) -> ReconReport:
    """Pay-by-link debits (P8.6): ask the provider about every open payment link and apply its payments through the
    same idempotent path webhooks use. The link → debit mapping is Nirantar's own record, so a payment is attributed
    to the right debit even if the provider did not copy the link's notes onto the payment."""
    report = ReconReport()
    now = (clock or SystemClock()).now()
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text(
            "SELECT r.request_id, r.provider_link_id, r.debit_id, r.invoice_id, r.invoice_ids, r.kind "
            "FROM billing.payment_requests r WHERE "
            "r.tenant_id=:t AND r.provider=:p AND r.status IN ('created','sent') "
            "AND (CAST(:d AS text) IS NULL OR r.debit_id=:d OR r.invoice_id=:d OR :d = ANY(r.invoice_ids))"),
            {"t": tenant_id, "p": provider.name, "d": debit_id}).all()
    for r in rows:
        try:
            if r.kind == "checkout":                     # a Nirantar pay page: the provider order holds the payments
                payments = provider.fetch_order_payments(r.provider_link_id)  # type: ignore[attr-defined]
                link_status = "created"
            else:
                link = provider.fetch_payment_link(r.provider_link_id)
                payments = [provider.fetch_payment(pid) for pid in link.payment_ids]
                link_status = link.status
        except ProviderError as exc:
            report.errors.append(f"{r.provider_link_id}: {exc}")
            continue
        paid_by: str | None = None
        for p in payments:
            report.scanned += 1
            p = dataclasses.replace(p, notes={**dict(p.notes),
                                              "nirantar_ref": r.debit_id or r.invoice_id or r.request_id})
            with tenant_tx(tenant_id, engine) as c:
                outcome = apply_payment(c, tenant_id, provider, p, now, None, clock)
            if outcome.event_type:
                report.changed += 1
                report.events.append(outcome.event_type)
            if outcome.event_type == "payment.captured" or p.status.value == "captured":
                paid_by = p.provider_payment_id
        status = "paid" if paid_by else {"expired": "expired", "cancelled": "cancelled"}.get(link_status)
        if status:
            with tenant_tx(tenant_id, engine) as c:
                c.execute(text("UPDATE billing.payment_requests SET status=:s, provider_payment_id=:pp, "
                               "paid_at=CASE WHEN :s='paid' THEN CAST(:n AS timestamptz) END "
                               "WHERE tenant_id=:t AND request_id=:r"),
                          {"s": status, "pp": paid_by, "n": now, "t": tenant_id, "r": r.request_id})
    return report
