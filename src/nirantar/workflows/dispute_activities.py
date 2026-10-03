"""Activities for DisputeWorkflow (kept out of the workflow module: Temporal's sandbox re-imports workflows)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.workflows.dispute import DisputeStep, dispute_case_id


@dataclass
class DisputeDeps:
    engine: Engine
    provider: Any                      # PaymentProvider | ProviderResolver
    llm: Any = None
    environment: str = "local"


class DisputeActivities:
    def __init__(self, deps: DisputeDeps) -> None:
        self.d = deps

    def _gw(self, step: DisputeStep) -> Any:
        from nirantar.core.clock import FixedClock
        from nirantar.mcp.gateway import ToolGateway
        from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
        from nirantar.payments.providers.resolver import provider_for

        return ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                           {"engine": self.d.engine, "provider": provider_for(self.d.provider, step.tenant_id)},
                           clock=FixedClock(datetime.fromisoformat(step.now_iso)), environment=self.d.environment)

    @activity.defn(name="dispute_prepare")
    def dispute_prepare(self, step: DisputeStep) -> dict[str, Any]:
        from nirantar.agents.dispute_defender import draft_representment, respond_by_ok, win_probability_prior
        from nirantar.db.session import tenant_tx
        from nirantar.disputes.evidence import build_evidence, store_evidence
        from nirantar.disputes.pack import render_pack
        from nirantar.evidence import objectstore
        from nirantar.payments.providers.resolver import provider_for
        from nirantar.settings import service as settings
        from nirantar.settings.schema import Operations

        now = datetime.fromisoformat(step.now_iso)
        case_id = dispute_case_id(step.dispute_id)
        provider = provider_for(self.d.provider, step.tenant_id)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            d = c.execute(text("SELECT * FROM billing.disputes WHERE tenant_id=:t AND dispute_id=:d"),
                          {"t": step.tenant_id, "d": step.dispute_id}).mappings().one()
            existing = c.execute(text("SELECT summary FROM ops.cases WHERE tenant_id=:t AND case_id=:c"),
                                 {"t": step.tenant_id, "c": case_id}).scalar_one_or_none()
            if existing:                                       # activity retry: never rebuild a stored pack
                summary = existing if isinstance(existing, dict) else json.loads(existing)
                return {"case_id": case_id, **{k: summary[k] for k in ("decision", "reason", "win_probability")},
                        "respond_by": d["respond_by"].isoformat()}
            items = build_evidence(c, step.tenant_id, step.dispute_id, provider)
            merchant: str = c.execute(text("SELECT name FROM core.tenants WHERE tenant_id=:t"),
                                 {"t": step.tenant_id}).scalar_one()
            min_win = settings.model(c, step.tenant_id, "operations", Operations).disputes.min_win_probability
        prob = win_probability_prior(items)
        text_, source = draft_representment(items, self.d.llm)
        pdf = render_pack(merchant=merchant, dispute=dict(d), items=items, summary=text_, generated_at=now)
        uri, sha = objectstore.put(step.tenant_id, "dispute_pack", pdf, "application/pdf")
        enough_time = respond_by_ok(d["respond_by"], now)
        decision = "contest" if prob >= min_win and enough_time else "escalate"
        reason = ("evidence supports the merchant" if decision == "contest" else
                  f"win probability {prob:.0%} below {min_win:.0%}" if enough_time
                  else "deadline too close for automatic submission")
        customer = None
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            if d["debit_id"]:
                customer = c.execute(text("SELECT customer_id FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                                     {"t": step.tenant_id, "d": d["debit_id"]}).scalar_one_or_none()
            c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, summary, "
                           "opened_at) VALUES (:t, :c, 'dispute', :s, :cu, 'open', CAST(:sum AS jsonb), :n) "
                           "ON CONFLICT DO NOTHING"),
                      {"t": step.tenant_id, "c": case_id, "s": step.dispute_id, "cu": customer or "unknown",
                       "sum": json.dumps({"pack_uri": uri, "pack_sha256": sha, "summary": text_, "draft_source": source,
                                          "win_probability": prob, "decision": decision, "reason": reason}), "n": now})
            if customer:
                store_evidence(c, step.tenant_id, case_id, customer, items, now)
            c.execute(text("UPDATE billing.disputes SET status='evidence_ready', updated_at=:n WHERE tenant_id=:t AND "
                           "dispute_id=:d AND status='open'"), {"n": now, "t": step.tenant_id, "d": step.dispute_id})
        return {"case_id": case_id, "decision": decision, "reason": reason, "win_probability": prob,
                "respond_by": d["respond_by"].isoformat(), "draft_source": source}

    @activity.defn(name="dispute_submit")
    def dispute_submit(self, step: DisputeStep) -> dict[str, Any]:
        r = self._gw(step).call(tenant_id=step.tenant_id, agent_id="dispute_defender",
                                tool_name="dispute.submit_representment",
                                args={"case_id": dispute_case_id(step.dispute_id)},
                                case_id=dispute_case_id(step.dispute_id))
        return {"status": r.status, "action_id": r.action_id, "approval_id": r.approval_id,
                "output": r.output, "error": r.error}

    @activity.defn(name="dispute_submitted")
    def dispute_submitted(self, step: DisputeStep) -> dict[str, Any]:
        """After a human approved, the approval inbox executed the tool: read the dispute's state."""
        from nirantar.db.session import tenant_tx

        with tenant_tx(step.tenant_id, self.d.engine) as c:
            status: str | None = c.execute(
                text("SELECT status FROM billing.disputes WHERE tenant_id=:t AND dispute_id=:d"),
                {"t": step.tenant_id, "d": step.dispute_id}).scalar_one()
        return {"status": "submitted" if status in ("submitted", "won", "lost") else "pending_approval"}

    @activity.defn(name="dispute_escalate")
    def dispute_escalate(self, step: DisputeStep) -> dict[str, Any]:
        from nirantar.db.session import tenant_tx

        with tenant_tx(step.tenant_id, self.d.engine) as c:
            c.execute(text("UPDATE ops.cases SET status='escalated', summary = summary || CAST(:s AS jsonb) WHERE "
                           "tenant_id=:t AND case_id=:c"),
                      {"s": json.dumps({"escalation": step.payload.get("reason")}), "t": step.tenant_id,
                       "c": dispute_case_id(step.dispute_id)})
        return {"escalated": True}

    @activity.defn(name="dispute_close")
    def dispute_close(self, step: DisputeStep) -> dict[str, Any]:
        from nirantar.core.ids import new_id
        from nirantar.db.session import tenant_tx

        now = datetime.fromisoformat(step.now_iso)
        outcome = str(step.payload.get("outcome"))
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            if outcome in ("won", "lost", "accepted"):
                c.execute(text("UPDATE billing.disputes SET status=:s, updated_at=:n WHERE tenant_id=:t AND "
                               "dispute_id=:d"), {"s": outcome, "n": now, "t": step.tenant_id, "d": step.dispute_id})
                c.execute(text("INSERT INTO ai.labels (tenant_id, label_id, prediction_id, subject_id, label_name, "
                               "value, source, observed_at) VALUES (:t, :l, NULL, :s, 'dispute_won', "
                               "CAST(:v AS jsonb), 'provider', :n)"),
                          {"t": step.tenant_id, "l": new_id("lbl"), "s": step.dispute_id,
                           "v": json.dumps({"value": outcome == "won"}), "n": now})
            c.execute(text("UPDATE ops.cases SET status='closed', closed_at=:n, summary = summary || CAST(:o AS jsonb) "
                           "WHERE tenant_id=:t AND case_id=:c"),
                      {"n": now, "o": json.dumps({"outcome": outcome}), "t": step.tenant_id,
                       "c": dispute_case_id(step.dispute_id)})
        return {"outcome": outcome}

    def all(self) -> list[Any]:
        return [self.dispute_prepare, self.dispute_submit, self.dispute_submitted, self.dispute_escalate,
                self.dispute_close]
