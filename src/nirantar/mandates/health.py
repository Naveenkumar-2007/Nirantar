"""Daily mandate health scan: every active subscription's mandate, diagnosed by the Mandate Doctor's rules against
the next debit. Facts come from the system of record (mandates verified with the provider, scheduled debits)."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.agents.mandate_doctor import MandateIn, diagnose
from nirantar.settings.schema import MandateHealth


def problem_key(mandate_id: str, repair: str, status: str, valid_until: date | None) -> str:
    """Same mandate + same problem → same key, so one problem gets one repair attempt (a new problem, e.g. a later
    expiry, gets a new one)."""
    raw = f"{mandate_id}:{repair}:{status}:{valid_until}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def scan(conn: Connection, tenant_id: str, today: date, cfg: MandateHealth) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT s.subscription_id, s.customer_id, s.amount_minor, m.mandate_id, m.rail, m.status, m.valid_until, "
        "m.max_amount_minor, m.failure_reason, "
        "(SELECT min(d.scheduled_for) FROM billing.debits d WHERE d.tenant_id=s.tenant_id AND "
        " d.subscription_id=s.subscription_id AND d.status IN ('scheduled','notified')) AS next_debit_on, "
        "(SELECT d.amount_minor FROM billing.debits d WHERE d.tenant_id=s.tenant_id AND "
        " d.subscription_id=s.subscription_id AND d.status IN ('scheduled','notified') "
        " ORDER BY d.scheduled_for LIMIT 1) AS next_amount "
        "FROM billing.subscriptions s JOIN billing.mandates m ON m.tenant_id=s.tenant_id AND m.mandate_id=s.mandate_id "
        "WHERE s.tenant_id=:t AND s.status='active'"), {"t": tenant_id}).all()
    horizon = max(cfg.lookahead_days, cfg.expiry_warn_days)
    out = []
    for r in rows:
        next_on = r.next_debit_on or today + timedelta(days=cfg.lookahead_days)
        plan = diagnose(MandateIn(mandate_id=r.mandate_id, rail=r.rail, status=r.status, valid_until=r.valid_until,
                                  max_amount_minor=r.max_amount_minor,
                                  next_debit_amount_minor=int(r.next_amount or r.amount_minor), next_debit_on=next_on,
                                  today=today, revoked_reason=r.failure_reason))
        if plan.repair == "none":
            continue
        if plan.urgency_days is not None and plan.urgency_days > horizon:
            continue
        out.append({"mandate_id": r.mandate_id, "subscription_id": r.subscription_id, "customer_id": r.customer_id,
                    "repair": plan.repair, "urgency_days": plan.urgency_days, "rationale": plan.rationale,
                    "customer_action_needed": plan.customer_action_needed, "next_debit_on": next_on.isoformat(),
                    "next_amount_minor": int(r.next_amount or r.amount_minor), "rail": r.rail,
                    "max_amount_minor": r.max_amount_minor,
                    "problem_key": problem_key(r.mandate_id, plan.repair, r.status, r.valid_until)})
    return out
