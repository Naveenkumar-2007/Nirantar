"""Read models for the dashboard. Every query runs on a tenant-scoped connection (RLS), so tenant filters in
SQL are defence-in-depth, not the isolation mechanism."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.experiments.service import analyze


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params).all()]


class BadCursor(ValueError):
    pass


def encode_cursor(ts: datetime, key: str) -> str:
    """Opaque, URL-safe keyset cursor (raw ISO timestamps contain '+', which URLs turn into spaces)."""
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{key}".encode()).decode().rstrip("=")


def _cursor_clause(cursor: str | None, col: str = "created_at", id_col: str = "id") -> tuple[str, dict[str, Any]]:
    if not cursor:
        return "", {}
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, key = raw.partition("|")
        parsed = datetime.fromisoformat(ts)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadCursor("invalid cursor") from exc
    return f" AND ({col}, {id_col}) < (:cur_ts, :cur_id)", {"cur_ts": parsed, "cur_id": key}


def page(rows: list[dict[str, Any]], limit: int, ts_key: str, id_key: str) -> dict[str, Any]:
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1][ts_key], rows[-1][id_key]) if more and rows else None
    return {"items": rows, "next_cursor": nxt}


def overview(conn: Connection, now: datetime) -> dict[str, Any]:
    debits = _rows(conn, "SELECT status, count(*) AS n, coalesce(sum(amount_minor),0) AS amount_minor "
                         "FROM billing.debits GROUP BY status")
    by = {r["status"]: r for r in debits}
    total = sum(r["n"] for r in debits)
    failed_ever: int = conn.execute(text(
        "SELECT count(DISTINCT debit_id) FROM billing.payments WHERE status='failed' AND debit_id IS NOT NULL"
    )).scalar_one()
    recovered = conn.execute(text(
        "SELECT count(*), coalesce(sum(value_minor),0) FROM experiments.outcomes WHERE verified AND "
            "outcome='recovered' "
        "AND value_minor > 0")).one()
    actions = _rows(conn, "SELECT status, count(*) AS n FROM ops.actions WHERE created_at > :since GROUP BY status",
                    since=now - timedelta(days=30))
    pending_approvals: int = conn.execute(text("SELECT count(*) FROM ops.approvals WHERE "
        "status='pending'")).scalar_one()
    exps = _rows(conn, "SELECT experiment_id, name FROM experiments.experiments ORDER BY created_at DESC LIMIT 1")
    incremental = analyze(conn, conn.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar_one(),
                          exps[0]["experiment_id"]) if exps else None
    open_cases: int = conn.execute(text("SELECT count(*) FROM ops.cases WHERE status <> 'closed'")).scalar_one()
    return {
        "debits": {"total": total, "by_status": by},
        "failed_debits": failed_ever,
        "recovered": {"count": recovered[0], "amount_minor": int(recovered[1])},
        "recovery_rate": (recovered[0] / failed_ever) if failed_ever else None,
        "agent_actions_30d": {r["status"]: r["n"] for r in actions},
        "pending_approvals": pending_approvals,
        "open_cases": open_cases,
        "experiment": {"id": exps[0]["experiment_id"], "name": exps[0]["name"], "analysis": incremental}
        if exps else None,
    }


def debits(conn: Connection, status: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
    cur, params = _cursor_clause(cursor, "d.created_at", "d.debit_id")
    where = " AND d.status = :status" if status else ""
    rows = _rows(conn, "SELECT d.debit_id, d.customer_id, c.display_name, d.scheduled_for, d.amount_minor, d.currency, "
                       "d.status, d.attempt_count, d.last_error_code, d.created_at FROM billing.debits d "
                       "JOIN billing.customers c ON c.tenant_id=d.tenant_id AND c.customer_id=d.customer_id "
                       f"WHERE true{where}{cur} ORDER BY d.created_at DESC, d.debit_id DESC LIMIT :lim",
                 status=status, lim=limit + 1, **params)
    return page(rows, limit, "created_at", "debit_id")


def debit_detail(conn: Connection, debit_id: str) -> dict[str, Any] | None:
    d = _rows(conn, "SELECT d.*, c.display_name, c.preferred_language, c.segment FROM billing.debits d JOIN "
                    "billing.customers c ON c.tenant_id=d.tenant_id AND c.customer_id=d.customer_id "
                    "WHERE d.debit_id=:d", d=debit_id)
    if not d:
        return None
    timeline: list[dict[str, Any]] = []
    for e in _rows(conn, "SELECT event_type, created_at, envelope FROM events.outbox WHERE subject_id=:d "
                         "ORDER BY created_at", d=debit_id):
        env = e["envelope"] if isinstance(e["envelope"], dict) else json.loads(e["envelope"])
        # business time (occurred_at), not outbox insert time, so events and actions interleave correctly
        timeline.append({"at": datetime.fromisoformat(env["occurred_at"]), "kind": "event",
                         "title": e["event_type"], "detail": env["payload"]})
    case = _rows(conn, "SELECT case_id, status, summary, opened_at, closed_at FROM ops.cases WHERE "
        "subject_id=:d", d=debit_id)
    if case:
        for a in _rows(conn, "SELECT action_id, agent_id, tool_name, status, policy_decision, result, created_at "
                             "FROM ops.actions WHERE case_id=:c ORDER BY created_at", c=case[0]["case_id"]):
            timeline.append({"at": a["created_at"], "kind": "action", "title": f"{a['agent_id']} → {a['tool_name']}",
                             "detail": {"status": a["status"], "policy": a["policy_decision"],
                                        "result": a["result"], "action_id": a["action_id"]}})
    preds = _rows(conn, "SELECT prediction_id, model_name, model_version, score, predicted_at FROM ai.predictions "
                        "WHERE subject_id=:d", d=debit_id)
    labels = _rows(conn, "SELECT label_name, value, observed_at FROM ai.labels WHERE subject_id=:d", d=debit_id)
    timeline.sort(key=lambda x: x["at"])
    return {"debit": d[0], "case": case[0] if case else None, "predictions": preds, "labels": labels,
            "timeline": timeline}


def agent_activity(conn: Connection, limit: int, cursor: str | None, agent: str | None) -> dict[str, Any]:
    cur, params = _cursor_clause(cursor, "created_at", "action_id")
    where = " AND agent_id=:agent" if agent else ""
    rows = _rows(conn, "SELECT action_id, case_id, agent_id, tool_name, status, policy_decision, policy_version, "
                       "approval_id, created_at, updated_at FROM ops.actions WHERE true"
                       f"{where}{cur} ORDER BY created_at DESC, action_id DESC LIMIT :lim",
                 agent=agent, lim=limit + 1, **params)
    return page(rows, limit, "created_at", "action_id")


def approvals(conn: Connection, status: str) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT a.approval_id, a.action_id, a.reason, a.status, a.requested_at, a.requested_by, "
                       "a.tool_name, a.expires_at, a.decided_by, a.decided_at, x.params, x.case_id FROM "
                           "ops.approvals a "
                       "LEFT JOIN ops.actions x ON x.tenant_id=a.tenant_id AND x.action_id=a.action_id "
                       "WHERE a.status=:s ORDER BY a.requested_at DESC LIMIT 200", s=status)


def compliance(conn: Connection, since: datetime) -> dict[str, Any]:
    denied = _rows(conn, "SELECT action_id, agent_id, tool_name, policy_decision, policy_version, created_at "
                         "FROM ops.actions WHERE policy_decision IN "
                             "('DENY','REQUIRE_APPROVAL','REQUIRE_MORE_INFORMATION') "
                         "AND created_at > :s ORDER BY created_at DESC LIMIT 200", s=since)
    contacts = _rows(conn, "SELECT channel, purpose, status, count(*) AS n FROM ops.contacts WHERE at > :s "
                           "GROUP BY 1,2,3 ORDER BY 4 DESC", s=since)
    return {"decisions": denied, "contact_log": contacts}


def audit(conn: Connection, limit: int) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT seq, at, actor, action, data_hash, prev_hash, hash FROM audit.records "
                       "ORDER BY seq DESC LIMIT :l", l=limit)


def customers(conn: Connection, limit: int, cursor: str | None) -> dict[str, Any]:
    cur, params = _cursor_clause(cursor, "created_at", "customer_id")
    rows = _rows(conn, "SELECT customer_id, external_ref, display_name, preferred_language, segment, consents, "
                       "(phone_enc IS NOT NULL) AS has_phone, created_at FROM billing.customers WHERE true"
                       f"{cur} ORDER BY created_at DESC, customer_id DESC LIMIT :lim", lim=limit + 1, **params)
    return page(rows, limit, "created_at", "customer_id")


# ---------------------------------------------------------------- customers 360 and conversations (P8.3, ADR-0020)
def customer_360(conn: Connection, tenant_id: str, customer_id: str) -> dict[str, Any] | None:
    """Everything known about one customer, from the system of record. Contact details stay encrypted; only
    whether they exist is shown."""
    c = _rows(conn, "SELECT customer_id, external_ref, display_name, preferred_language, timezone, segment, consents, "
                    "(phone_enc IS NOT NULL) AS has_phone, (email_enc IS NOT NULL) AS has_email, created_at "
                    "FROM billing.customers WHERE customer_id=:c", c=customer_id)
    if not c:
        return None
    return {
        "customer": c[0],
        "subscriptions": _rows(conn, "SELECT s.subscription_id, s.provider, s.status, s.amount_minor, s.currency, "
                                     "s.interval, s.next_charge_on, s.mandate_id, s.created_at, s.collection_method, "
                                     "s.plan_id, p.name AS plan_name FROM billing.subscriptions s "
                                     "LEFT JOIN billing.plans "
                                     "p ON p.tenant_id=s.tenant_id AND p.plan_id=s.plan_id WHERE s.customer_id=:c "
                                     "ORDER BY s.created_at DESC", c=customer_id),
        "payment_requests": _rows(conn, "SELECT request_id, debit_id, url, amount_minor, status, created_at, sent_at, "
                                        "paid_at FROM billing.payment_requests WHERE customer_id=:c "
                                        "ORDER BY created_at "
                                        "DESC LIMIT 20", c=customer_id),
        "mandates": _rows(conn, "SELECT mandate_id, rail, status, max_amount_minor, valid_until, failure_reason, "
                                "last_verified_at FROM billing.mandates WHERE customer_id=:c ORDER BY created_at DESC",
                          c=customer_id),
        "debits": _rows(conn, "SELECT debit_id, scheduled_for, amount_minor, status, attempt_count, last_error_code "
                              "FROM billing.debits WHERE customer_id=:c ORDER BY scheduled_for DESC LIMIT 24",
                        c=customer_id),
        "cases": _rows(conn, "SELECT case_id, kind, status, opened_at, closed_at, summary->>'outcome' AS outcome "
                             "FROM ops.cases WHERE customer_id=:c ORDER BY opened_at DESC LIMIT 20", c=customer_id),
        "contacts": _rows(conn, "SELECT channel, purpose, status, at FROM ops.contacts WHERE customer_id=:c "
                                "ORDER BY at DESC LIMIT 30", c=customer_id),
        "actions": _rows(conn, "SELECT a.action_id, a.agent_id, a.tool_name, a.status, a.policy_decision, a.created_at "
                               "FROM ops.actions a WHERE a.params->>'customer_id' = :c OR a.case_id IN (SELECT case_id "
                               "FROM ops.cases WHERE customer_id=:c) ORDER BY a.created_at DESC LIMIT 30",
                         c=customer_id),
        "promises": _rows(conn, "SELECT promise_id, debit_id, promised_date, source, quote, status, reminder_sent_at, "
                                "created_at, resolved_at FROM ops.promises WHERE customer_id=:c ORDER BY created_at "
                                "DESC LIMIT 20", c=customer_id),
        "replies": _rows(conn, "SELECT value->>'intent' AS intent, value->>'promised_date' AS promised_date, "
                               "created_at FROM ai.memory WHERE subject_id=:c AND key='customer_reply' "
                               "ORDER BY created_at DESC LIMIT 10", c=customer_id),
    }


def conversations(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT DISTINCT ON (m.customer_id) m.customer_id, c.display_name, c.preferred_language, "
                       "m.direction, m.kind, m.status, m.created_at AS last_at, "
                       "(SELECT max(x.created_at) FROM comms.messages x WHERE x.customer_id=m.customer_id AND "
                       " x.direction='inbound') AS last_inbound_at, "
                       "(SELECT count(*) FROM comms.messages x WHERE x.customer_id=m.customer_id) AS messages "
                       "FROM comms.messages m JOIN billing.customers c ON c.customer_id=m.customer_id "
                       "WHERE m.customer_id IS NOT NULL ORDER BY m.customer_id, m.created_at DESC")
    rows.sort(key=lambda r: r["last_at"], reverse=True)          # most recent conversation first
    return rows[:limit]


def thread(conn: Connection, tenant_id: str, customer_id: str) -> list[dict[str, Any]]:
    from nirantar.core import crypto

    rows = _rows(conn, "SELECT message_id, direction, kind, template_ref, status, error_title, body_enc, evidence_id, "
                       "created_at FROM comms.messages WHERE customer_id=:c ORDER BY created_at, message_id",
                 c=customer_id)
    for r in rows:
        blob = r.pop("body_enc", None)
        r["text"] = crypto.decrypt(bytes(blob), tenant_id) if blob else None
    return rows


def today(conn: Connection, now: datetime, tz: str = "Asia/Kolkata") -> dict[str, Any]:
    """The owner's day in one call: money in, money due, money stuck, promises, outages and decisions waiting.
    "Today" is the business's calendar day (IST by default), not UTC."""
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(tz))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    d0 = day_start.date()

    def one(sql: str, **p: Any) -> Any:
        return conn.execute(text(sql), p).one()

    collected = one("SELECT count(*) AS n, coalesce(sum(amount_minor),0) AS minor FROM billing.payments WHERE "
                    "status='captured' AND coalesce(provider_created_at, created_at) >= :s", s=day_start)
    due = {label: one("SELECT count(*) AS n, coalesce(sum(amount_minor),0) AS minor FROM billing.debits WHERE "
                      "scheduled_for=:d AND status IN ('scheduled','notified','attempting')", d=d)
           for label, d in (("today", d0), ("tomorrow", d0 + timedelta(days=1)))}
    overdue = one("SELECT count(*) AS n, coalesce(sum(amount_minor),0) AS minor FROM billing.debits WHERE "
                  "status='failed' OR (status IN ('scheduled','notified','attempting') AND scheduled_for < :d)", d=d0)
    promises_today = one("SELECT count(*) AS n FROM ops.promises WHERE status='open' AND promised_date=:d", d=d0)
    broken_week = one("SELECT count(*) AS n FROM ops.promises WHERE status='broken' AND resolved_at >= :s",
                      s=day_start - timedelta(days=7))
    approvals = one("SELECT count(*) AS n FROM ops.approvals WHERE status='pending'")
    incidents = one("SELECT count(*) AS n FROM core.payment_incidents WHERE status='open'")
    recovered_week = one("SELECT count(*) AS n, coalesce(sum(value_minor),0) AS minor FROM experiments.outcomes "
                         "WHERE verified AND outcome='recovered' AND value_minor > 0 AND observed_at >= :s",
                         s=day_start - timedelta(days=7))
    return {
        "date": d0.isoformat(), "timezone": tz,
        "collected_today": {"n": collected.n, "minor": int(collected.minor)},
        "due_today": {"n": due["today"].n, "minor": int(due["today"].minor)},
        "due_tomorrow": {"n": due["tomorrow"].n, "minor": int(due["tomorrow"].minor)},
        "overdue": {"n": overdue.n, "minor": int(overdue.minor)},
        "recovered_7d": {"n": recovered_week.n, "minor": int(recovered_week.minor)},
        "promises_due_today": promises_today.n, "promises_broken_7d": broken_week.n,
        "approvals_waiting": approvals.n, "live_incidents": incidents.n,
    }
