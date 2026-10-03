"""Activities for RevivalWorkflow. Every customer/money side effect goes through the MCP ToolGateway
(scope → schema → Compliance Guardian → approval → execute → audit); time comes from the workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.core.clock import FixedClock
from nirantar.db.session import tenant_tx
from nirantar.experiments import service as experiments
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.domain import PaymentProvider
from nirantar.payments.providers.resolver import ProviderResolver, provider_for
from nirantar.settings import templates
from nirantar.workflows.revival import RevivalStep

AGENT = "revival_agent"


@dataclass
class RevivalDeps:
    engine: Engine
    provider: PaymentProvider | ProviderResolver     # services pass a resolver: each tenant's own account
    comms: Any
    environment: str = "local"


class RevivalActivities:
    def __init__(self, deps: RevivalDeps) -> None:
        self.d = deps

    def _case(self, step: RevivalStep) -> dict[str, Any]:
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            row = c.execute(text("SELECT customer_id, subject_id, summary, opened_at, status FROM ops.cases WHERE "
                                 "tenant_id=:t AND case_id=:c AND kind='revival'"),
                            {"t": step.tenant_id, "c": step.case_id}).one()
        summary = row.summary if isinstance(row.summary, dict) else json.loads(row.summary)
        return {**summary, "customer_id": row.customer_id, "entity_id": row.subject_id, "opened_at": row.opened_at,
                "status": row.status}

    def _gw(self, step: RevivalStep, experiment_id: str) -> ToolGateway:
        return ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                           {"engine": self.d.engine, "provider": provider_for(self.d.provider, step.tenant_id),
                            "comms": self.d.comms,
                            "experiment_id": experiment_id},
                           clock=FixedClock(datetime.fromisoformat(step.now_iso)), environment=self.d.environment)

    def _call(self, step: RevivalStep, case: dict[str, Any], tool: str, args: dict[str, Any]) -> Any:
        return self._gw(step, case["experiment_id"]).call(tenant_id=step.tenant_id, agent_id=AGENT, tool_name=tool,
                                                          args=args, case_id=step.case_id)

    @activity.defn(name="revival_start")
    def revival_start(self, step: RevivalStep) -> dict[str, Any]:
        case = self._case(step)
        if case["arm"] == experiments.HOLDOUT:
            self._call(step, case, "experiment.log_exposure", {"customer_id": case["customer_id"], "arm": "holdout"})
            return {"status": "holdout", "arm": "holdout"}
        r = self._call(step, case, "retention.create_offer", {"case_id": step.case_id})
        return {"status": r.status, "arm": case["arm"], "offer_id": r.output.get("offer_id"),
                "approval_id": r.approval_id, "policy": [h.policy_id for h in r.decision.hits] if r.decision else []}

    @activity.defn(name="revival_offer_ready")
    def revival_offer_ready(self, step: RevivalStep) -> dict[str, Any]:
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            ok = c.execute(text("SELECT link_ref IS NOT NULL FROM billing.offers WHERE tenant_id=:t AND case_id=:c"),
                           {"t": step.tenant_id, "c": step.case_id}).scalar_one_or_none()
        return {"ready": bool(ok), "status": "executed" if ok else "pending_approval"}

    @activity.defn(name="revival_send")
    def revival_send(self, step: RevivalStep) -> dict[str, Any]:
        case = self._case(step)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            offer = c.execute(text("SELECT link_ref, discount_pct FROM billing.offers WHERE tenant_id=:t AND "
                                   "case_id=:c"), {"t": step.tenant_id, "c": step.case_id}).one()
        profile = self._call(step, case, "customer.get_profile", {"customer_id": case["customer_id"]}).output
        lang = profile.get("language") or "en"
        key = "offer.discount_pct" if offer.discount_pct else "offer.reminder"
        phrase = self._call(step, case, "content.get_template", {"key": key, "language": lang}).output
        body = self._call(step, case, "content.get_template", {"key": "whatsapp.winback", "language": lang}).output
        offer_text = templates.render(phrase["body"], {"pct": str(offer.discount_pct)})
        text_ = templates.render(body["body"], {"name": profile.get("first_name", ""), "plan": case.get("plan")
                                                or "your subscription", "offer": offer_text, "link": offer.link_ref})
        r = self._call(step, case, "comms.send_winback", {"case_id": step.case_id, "text": text_,
                                                          "offer": offer_text})
        if r.status == "executed":
            self._call(step, case, "experiment.log_exposure", {"customer_id": case["customer_id"], "arm": case["arm"],
                                                               "action_ref": r.action_id})
        retry = r.decision.retry_after if r.decision is not None else None
        return {"status": r.status, "template_refs": [phrase["ref"], body["ref"]],
                "denied_by": [h.policy_id for h in r.decision.hits] if r.decision else [],
                "retry_after": retry.isoformat() if retry else None}

    @activity.defn(name="revival_check")
    def revival_check(self, step: RevivalStep) -> dict[str, Any]:
        """Same measurement in every arm: a captured payment by the customer after the case opened (provider-
        verified at ingestion), or the offer redeemed."""
        case = self._case(step)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            row = c.execute(text(
                "SELECT p.provider_payment_id, p.amount_minor FROM billing.payments p WHERE p.tenant_id=:t AND "
                "p.customer_id=:c AND p.status='captured' AND coalesce(p.provider_created_at, p.created_at) >= :opened "
                "ORDER BY p.created_at LIMIT 1"),
                {"t": step.tenant_id, "c": case["customer_id"], "opened": case["opened_at"]}).first()
        return {"reactivated": row is not None, "payment": row.provider_payment_id if row else None,
                "amount_minor": int(row.amount_minor) if row else 0}

    @activity.defn(name="revival_close")
    def revival_close(self, step: RevivalStep) -> dict[str, Any]:
        case = self._case(step)
        if case["status"] == "closed":
            return {"outcome": "already_closed"}
        now = datetime.fromisoformat(step.now_iso)
        chk = self.revival_check(step) if step.payload.get("reactivated") else {"reactivated": False,
                                                                                 "amount_minor": 0}
        outcome = "reactivated" if chk["reactivated"] else "not_reactivated"
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            experiments.record_outcome(c, step.tenant_id, case["experiment_id"], case["customer_id"], None, outcome,
                                       chk["amount_minor"], True, now)
            c.execute(text("UPDATE billing.offers SET status='expired' WHERE tenant_id=:t AND case_id=:c AND "
                           "status IN ('created','sent')"), {"t": step.tenant_id, "c": step.case_id})
            c.execute(text("UPDATE ops.cases SET status='closed', closed_at=:n, summary = summary || "
                           "CAST(:o AS jsonb) WHERE tenant_id=:t AND case_id=:c"),
                      {"n": now, "o": json.dumps({"outcome": outcome, "reason": step.payload.get("reason"),
                                                  "value_minor": chk["amount_minor"]}),
                       "t": step.tenant_id, "c": step.case_id})
        return {"outcome": outcome, "value_minor": chk["amount_minor"], "reason": step.payload.get("reason")}

    def all(self) -> list[Any]:
        return [self.revival_start, self.revival_offer_ready, self.revival_send, self.revival_check,
                self.revival_close]
