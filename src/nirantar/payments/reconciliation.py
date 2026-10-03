"""Reconciliation poller: provider state is the source of truth; webhooks are an optimisation.

Heals missed/disabled webhooks (Razorpay disables after 24h of failures; Cashfree
can't replay subscription webhooks) by pulling provider payments and applying
the same idempotent `apply_payment` path that webhooks use.
"""

from __future__ import annotations

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
