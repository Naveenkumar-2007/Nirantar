"""Plans, enrollment and the billing schedule (P8.6, ADR-0022).

Nirantar owns the schedule: every active subscription has `next_charge_on`; the daily sweep (and enrollment itself)
creates the debit for each due date inside the horizon and advances the date by the plan interval. Creating a debit
emits `subscription.debit_scheduled`, which starts DebitCycleWorkflow — the same path every debit already takes.

Collection methods (per subscription, defaulting from the plan):
  payment_link           on the due date Nirantar sends a payment link (works on every provider account)
  mandate                Nirantar charges a registered UPI AutoPay / e-mandate token (needs the provider's
                         recurring-payments feature)
  provider_subscription  the provider runs the schedule and charges (e.g. Razorpay Subscriptions)
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.billing.service import schedule_debit
from nirantar.core.ids import new_id
from nirantar.core.money import Money

INTERVALS = ("weekly", "monthly", "quarterly", "yearly")
METHODS = ("payment_link", "mandate", "provider_subscription")
NIRANTAR_RUN = ("payment_link", "mandate")       # methods where Nirantar, not the provider, creates the debits
AVAILABLE = ("payment_link",)                    # mandate charging is not built yet: never offer what cannot collect
HORIZON_DAYS = 3                                  # debits exist from T-3, when the workflow predicts and notifies
MAX_CATCH_UP = 3                                  # a sweep never creates more than this many debits per subscription


class PlanError(ValueError):
    pass


def add_interval(d: date, interval: str, n: int = 1) -> date:
    if interval == "weekly":
        return d + timedelta(weeks=n)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[interval] * n
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))   # 31 Jan → 28/29 Feb


@dataclass(frozen=True)
class NewPlan:
    name: str
    amount: Money
    interval: str
    collection_method: str = "payment_link"
    description: str | None = None


def create_plan(conn: Connection, tenant_id: str, p: NewPlan, actor: str) -> str:
    name = p.name.strip()
    if not 2 <= len(name) <= 80:
        raise PlanError("plan name must be 2-80 characters")
    if p.interval not in INTERVALS:
        raise PlanError(f"interval must be one of {INTERVALS}")
    if p.collection_method not in METHODS:
        raise PlanError(f"collection method must be one of {METHODS}")
    if p.collection_method not in AVAILABLE:
        raise PlanError("UPI AutoPay / e-mandate collection is not available yet: Nirantar does not charge mandate "
                        "tokens until the provider's recurring-payments feature is enabled and integrated")
    if p.amount.currency != "INR" or not 100 <= p.amount.minor <= 10_000_000:      # ₹1 – ₹1,00,000
        raise PlanError("amount must be between ₹1 and ₹1,00,000")
    if conn.execute(text("SELECT 1 FROM billing.plans WHERE lower(name)=lower(:n) AND active"), {"n": name}).first():
        raise PlanError("an active plan with this name already exists")
    pid = new_id("pln")
    conn.execute(text("INSERT INTO billing.plans (tenant_id, plan_id, name, description, amount_minor, currency, "
                      "interval, collection_method, created_by) VALUES (:t, :p, :n, :d, :a, 'INR', :i, :m, :by)"),
                 {"t": tenant_id, "p": pid, "n": name, "d": (p.description or "")[:300] or None, "a": p.amount.minor,
                  "i": p.interval, "m": p.collection_method, "by": actor})
    return pid


def list_plans(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT p.plan_id, p.name, p.description, p.amount_minor, p.interval, p.collection_method, p.active, "
        "p.created_at, count(s.subscription_id) FILTER (WHERE s.status='active') AS active_subscriptions "
        "FROM billing.plans p LEFT JOIN billing.subscriptions s ON s.tenant_id=p.tenant_id AND s.plan_id=p.plan_id "
        "GROUP BY p.tenant_id, p.plan_id ORDER BY p.active DESC, p.created_at DESC")).mappings().all()
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


def enroll(conn: Connection, tenant_id: str, customer_id: str, plan_id: str, start_on: date, now: datetime,
           collection_method: str | None = None) -> dict[str, Any]:
    plan = conn.execute(text("SELECT * FROM billing.plans WHERE plan_id=:p AND active"), {"p": plan_id}).one_or_none()
    if plan is None:
        raise PlanError("no such active plan")
    method = collection_method or plan.collection_method
    if method not in NIRANTAR_RUN:
        raise PlanError("provider-run subscriptions are created at the provider and imported, not enrolled here")
    if start_on < now.date():
        raise PlanError("the first charge cannot be in the past")
    if start_on > now.date() + timedelta(days=366):
        raise PlanError("the first charge must be within a year")
    if conn.execute(text("SELECT 1 FROM billing.subscriptions WHERE customer_id=:c AND plan_id=:p AND status IN "
                         "('created','active','paused')"), {"c": customer_id, "p": plan_id}).first():
        raise PlanError("this customer is already on this plan")
    if conn.execute(text("SELECT 1 FROM billing.customers WHERE customer_id=:c"), {"c": customer_id}).first() is None:
        raise PlanError("no such customer")
    provider = conn.execute(text("SELECT provider FROM core.provider_accounts WHERE tenant_id=:t "
                                 "ORDER BY (mode='live') DESC, created_at DESC LIMIT 1"),
                            {"t": tenant_id}).scalar_one_or_none()
    if provider is None:
        raise PlanError("connect a payment provider first")
    sid = new_id("sub")
    conn.execute(text("INSERT INTO billing.subscriptions (tenant_id, subscription_id, customer_id, provider, "
                      "amount_minor, currency, interval, status, next_charge_on, plan_id, collection_method) "
                      "VALUES (:t, :s, :c, :pr, :a, 'INR', :i, 'active', :n, :p, :m)"),
                 {"t": tenant_id, "s": sid, "c": customer_id, "pr": provider, "a": plan.amount_minor,
                  "i": plan.interval, "n": start_on, "p": plan_id, "m": method})
    created = ensure_debits(conn, tenant_id, now.date(), now, subscription_id=sid)
    return {"subscription_id": sid, "collection_method": method, "next_charge_on": start_on.isoformat(),
            "debits_created": created}


def ensure_debits(conn: Connection, tenant_id: str, today: date, now: datetime,
                  subscription_id: str | None = None) -> list[str]:
    """Create every missing debit due within the horizon for Nirantar-run subscriptions; advance next_charge_on."""
    horizon = today + timedelta(days=HORIZON_DAYS)
    subs = conn.execute(text(
        "SELECT subscription_id, next_charge_on, interval FROM billing.subscriptions WHERE status='active' AND "
        "collection_method = ANY(:m) AND next_charge_on IS NOT NULL AND next_charge_on <= :h "
        "AND (CAST(:s AS text) IS NULL OR subscription_id = :s) FOR UPDATE"),
        {"m": list(NIRANTAR_RUN), "h": horizon, "s": subscription_id}).all()
    created: list[str] = []
    for s in subs:
        due: date = s.next_charge_on
        for _ in range(MAX_CATCH_UP):
            if due > horizon:
                break
            exists = conn.execute(text("SELECT 1 FROM billing.debits WHERE subscription_id=:s AND scheduled_for=:d"),
                                  {"s": s.subscription_id, "d": due}).first()
            if not exists and due >= today:          # a missed past date is not charged retroactively
                created.append(schedule_debit(conn, tenant_id, s.subscription_id, due, now))
            due = add_interval(due, s.interval)
        conn.execute(text("UPDATE billing.subscriptions SET next_charge_on=:n, updated_at=:now WHERE "
                          "subscription_id=:s"), {"n": due, "now": now, "s": s.subscription_id})
    return created


def cancel(conn: Connection, subscription_id: str, now: datetime) -> int:
    """Cancel a Nirantar-run subscription and every debit that has not been attempted yet."""
    n = conn.execute(text("UPDATE billing.subscriptions SET status='cancelled', next_charge_on=NULL, updated_at=:n "
                          "WHERE subscription_id=:s AND status IN ('created','active','paused')"),
                     {"n": now, "s": subscription_id}).rowcount
    if not n:
        raise PlanError("subscription is not active")
    return int(conn.execute(text("UPDATE billing.debits SET status='cancelled', updated_at=:n WHERE "
                                 "subscription_id=:s AND status IN ('scheduled','notified')"),
                            {"n": now, "s": subscription_id}).rowcount)
