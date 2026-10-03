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
