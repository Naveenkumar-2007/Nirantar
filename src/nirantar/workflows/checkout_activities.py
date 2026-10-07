"""Activities for CheckoutRecoveryWorkflow (ADR-0028)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.checkout import service as checkouts
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.resolver import provider_for
from nirantar.workflows.checkout import CheckoutStep

MIN_AMOUNT_MINOR = 100_00               # below ₹100 a reminder costs more goodwill than it recovers
ESCALATE_MIN_MINOR = 10_000_00          # ₹10,000+: a person follows up after the automated steps


@dataclass
class CheckoutDeps:
    engine: Engine
    provider: Any
    comms: Any
    environment: str = "local"


class CheckoutActivities:
    def __init__(self, deps: CheckoutDeps) -> None:
        self.d = deps

    @activity.defn(name="checkout_poll")
    def checkout_poll(self, step: CheckoutStep) -> dict[str, Any]:
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            r = c.execute(text("SELECT status, created_at, last_activity_at FROM billing.checkout_sessions WHERE "
                               "tenant_id=:t AND session_id=:s"), {"t": step.tenant_id, "s": step.session_id}).one()
        return {"status": r.status, "created_at": r.created_at.isoformat(),
                "last_activity_at": r.last_activity_at.isoformat()}

    @activity.defn(name="checkout_assess")
    def checkout_assess(self, step: CheckoutStep) -> dict[str, Any]:
        now = datetime.fromisoformat(step.now_iso)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            a = checkouts.assess(c, step.tenant_id, step.session_id, now, MIN_AMOUNT_MINOR)
        return {**a, "escalate": a["amount_minor"] >= ESCALATE_MIN_MINOR}

    @activity.defn(name="checkout_step")
    def checkout_step(self, step: CheckoutStep) -> dict[str, Any]:
        now = datetime.fromisoformat(step.now_iso)
        if step.step in ("held_out", "ineligible"):
            with tenant_tx(step.tenant_id, self.d.engine) as c:
                checkouts.record_step(c, step.tenant_id, step.session_id, step.step, step.step, None, {}, now)
            return {"status": step.step}
        if step.step == "expire":
            with tenant_tx(step.tenant_id, self.d.engine) as c:
                checkouts.close(c, step.tenant_id, step.session_id, "expired", "no payment within 72 hours", now)
            return {"status": "expired"}
        if step.step == "escalate":
            with tenant_tx(step.tenant_id, self.d.engine) as c:
                s = c.execute(text("SELECT customer_id, checkout_ref, amount_minor, cause FROM "
                                   "billing.checkout_sessions WHERE tenant_id=:t AND session_id=:s"),
                              {"t": step.tenant_id, "s": step.session_id}).one()
                c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, "
                               "summary, opened_at) VALUES (:t, :c, 'checkout', :s, :cu, 'escalated', "
                               "CAST(:sum AS jsonb), :n) ON CONFLICT DO NOTHING"),
                          {"t": step.tenant_id, "c": new_id("cas"), "s": step.session_id, "cu": s.customer_id,
                           "sum": json.dumps({"checkout": s.checkout_ref, "amount_minor": int(s.amount_minor),
                                              "cause": s.cause, "reason": "high-value checkout still open after "
                                                                          "the reminder and the follow-up"}),
                           "n": now})
                checkouts.record_step(c, step.tenant_id, step.session_id, "escalate", "executed", None,
                                      {"case": "checkout"}, now)
            return {"status": "escalated"}
        gw = ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                         {"engine": self.d.engine, "comms": self.d.comms,
                          "provider": provider_for(self.d.provider, step.tenant_id)},
                         clock=FixedClock(now), environment=self.d.environment)
        r = gw.call(tenant_id=step.tenant_id, agent_id="checkout_agent", tool_name="billing.send_checkout_recovery",
                    args={"session_id": step.session_id, "step": step.step},
                    idempotency_key=f"checkout:{step.session_id}:{step.step}")
        status = "executing" if r.status == "in_progress" else r.status
        detail = {"messages": [h.message for h in r.decision.hits] if r.decision else [], "sent": r.output.get("sent"),
                  "template": r.output.get("template"), "error": r.error or r.output.get("channel_error"),
                  "retry_after": r.decision.retry_after.isoformat() if r.decision and r.decision.retry_after else None}
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            checkouts.record_step(c, step.tenant_id, step.session_id, step.step, status, r.action_id, detail, now)
        return {"status": status, **detail}

    def all(self) -> list[Any]:
        return [self.checkout_poll, self.checkout_assess, self.checkout_step]
