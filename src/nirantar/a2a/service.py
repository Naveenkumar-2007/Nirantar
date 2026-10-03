"""A2A task service (tenant-scoped, durable) and the two Nirantar skills:

1. collections.agency_handoff (outbound): a lender hands a delinquent loan to an external collection agency's
   agent. The agency streams contact attempts back; each one is checked against the lender's recovery rules
   (RBI hours) and written to the contact log — the digital record RBI expects.
2. subscription.customer_request (inbound): a customer's own AI agent asks to pause a subscription or move a
   debit date. The request must carry a customer consent reference and passes the Compliance Guardian.
"""

from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.a2a.protocol import (
    TERMINAL,
    A2AError,
    AgentCard,
    SignedMessage,
    TaskState,
    check_transition,
    verify,
)
from nirantar.core.ids import new_id

RECOVERY_WINDOW = (time(8, 0), time(19, 0))   # IN-RBI-RBC-RECOVERY-HOURS-001


def trust(conn: Connection, tenant_id: str, card: AgentCard) -> None:
    conn.execute(text("INSERT INTO ops.a2a_trusted_agents (tenant_id, agent_name, organization, public_key, "
                      "skills, card) VALUES (:t, :n, :o, :k, :s, CAST(:c AS jsonb)) "
                      "ON CONFLICT (tenant_id, agent_name) DO UPDATE SET public_key=EXCLUDED.public_key, "
                      "skills=EXCLUDED.skills, card=EXCLUDED.card, revoked_at=NULL"),
                 {"t": tenant_id, "n": card.name, "o": card.organization, "k": card.public_key, "s": card.skills,
                  "c": card.model_dump_json()})


def revoke(conn: Connection, tenant_id: str, agent_name: str, now: datetime) -> None:
    conn.execute(text("UPDATE ops.a2a_trusted_agents SET revoked_at=:n WHERE tenant_id=:t AND agent_name=:a"),
                 {"n": now, "t": tenant_id, "a": agent_name})


def accept(conn: Connection, tenant_id: str, me: str, msg: SignedMessage, now: datetime) -> AgentCard:
    """Authenticate a message; record it (nonce uniqueness = replay protection). Raises A2AError."""
    row = conn.execute(text("SELECT card, revoked_at FROM ops.a2a_trusted_agents WHERE tenant_id=:t AND agent_name=:a"),
                       {"t": tenant_id, "a": msg.sender}).one_or_none()
    if row is None or row.revoked_at is not None:
        raise A2AError(f"untrusted agent {msg.sender}")
    card = AgentCard.model_validate(row.card if isinstance(row.card, dict) else json.loads(row.card))
    verify(msg, card, me, now)
    inserted = conn.execute(
        text("INSERT INTO ops.a2a_messages (tenant_id, message_id, task_id, sender, nonce, kind, body, signature, "
             "verified, received_at) VALUES (:t, :m, :task, :s, :n, :k, CAST(:b AS jsonb), :sig, true, :now) "
             "ON CONFLICT (tenant_id, sender, nonce) DO NOTHING RETURNING message_id"),
        {"t": tenant_id, "m": new_id("a2m"), "task": msg.task_id, "s": msg.sender, "n": msg.nonce, "k": msg.kind,
         "b": json.dumps(msg.body), "sig": msg.signature, "now": now}).one_or_none()
    if inserted is None:
        raise A2AError("replayed message (nonce already seen)")
    return card


def create_task(conn: Connection, tenant_id: str, skill: str, counterparty: str, direction: str,
                request: dict[str, Any], task_id: str | None = None) -> str:
    tid = task_id or new_id("tsk")
    conn.execute(text("INSERT INTO ops.a2a_tasks (tenant_id, task_id, skill, counterparty, direction, state, request) "
                      "VALUES (:t, :id, :s, :c, :d, 'submitted', CAST(:r AS jsonb))"),
                 {"t": tenant_id, "id": tid, "s": skill, "c": counterparty, "d": direction, "r": json.dumps(request)})
    return tid


def transition(conn: Connection, tenant_id: str, task_id: str, new: TaskState, result: dict[str, Any] | None = None
               ) -> TaskState:
    cur = TaskState(conn.execute(text("SELECT state FROM ops.a2a_tasks WHERE tenant_id=:t AND task_id=:id FOR UPDATE"),
                                 {"t": tenant_id, "id": task_id}).scalar_one())
    check_transition(cur, new)
    conn.execute(text("UPDATE ops.a2a_tasks SET state=:s, result=coalesce(CAST(:r AS jsonb), result), updated_at=now() "
                      "WHERE tenant_id=:t AND task_id=:id"),
                 {"s": new.value, "r": json.dumps(result) if result is not None else None, "t": tenant_id,
                  "id": task_id})
    return new


# ---------------------------------------------------------------- skill 1: agency handoff (outbound)
def handoff_request(loan_id: str, dpd: int, emi_minor: int, grid: list[str], ptp_history: list[dict[str, Any]]
                    ) -> dict[str, Any]:
    return {"loan_id": loan_id, "dpd": dpd, "emi_minor": emi_minor, "allowed_hours_local": ["08:00", "19:00"],
            "timezone": "Asia/Kolkata", "restructure_grid": grid, "ptp_history": ptp_history,
            "rules": ["no third-party contact", "disclose agent identity", "log every attempt"]}


def record_agency_attempt(conn: Connection, tenant_id: str, task_id: str, customer_id: str, attempt: dict[str, Any]
                          ) -> dict[str, Any]:
    """Validate an agency's reported contact attempt against the lender's rules and log it."""
    at = datetime.fromisoformat(attempt["at"])
    local = at.astimezone(ZoneInfo("Asia/Kolkata")).time()
    violations = []
    if not (RECOVERY_WINDOW[0] <= local < RECOVERY_WINDOW[1]):
        violations.append("IN-RBI-RBC-RECOVERY-HOURS-001")
    if attempt.get("contacted_party", "borrower") != "borrower":
        violations.append("IN-RBI-RBC-RECOVERY-CONDUCT-001")
    conn.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                      "VALUES (:t, :c, :cu, :ch, 'recovery', :s, :at)"),
                 {"t": tenant_id, "c": new_id("cnt"), "cu": customer_id, "ch": attempt.get("channel", "voice"),
                  "s": "violation" if violations else attempt.get("outcome", "attempted"), "at": at})
    return {"task_id": task_id, "violations": violations, "logged": True}


# ---------------------------------------------------------------- skill 2: customer's agent (inbound)
def handle_customer_request(body: dict[str, Any]) -> tuple[TaskState, dict[str, Any]]:
    """Decide the state for a customer-agent request. Execution of an accepted request goes through MCP."""
    if not body.get("consent_ref"):
        return TaskState.INPUT_REQUIRED, {"needs": "consent_ref",
                                          "message": "customer consent reference required for delegated requests"}
    action = body.get("action")
    if action == "pause_subscription":
        months = int(body.get("months", 1))
        if not 1 <= months <= 3:
            return TaskState.INPUT_REQUIRED, {"needs": "months", "allowed": [1, 2, 3]}
        return TaskState.WORKING, {"accepted": "pause_subscription", "months": months}
    if action == "move_debit_date":
        return TaskState.WORKING, {"accepted": "move_debit_date", "requested_day": body.get("day"),
                                   "note": "new date is set by the policy engine (fresh pre-debit notice required)"}
    return TaskState.FAILED, {"error": f"unsupported action {action!r}"}


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL
