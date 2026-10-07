"""Activities for InvoiceChaseWorkflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.resolver import provider_for
from nirantar.payments.reconciliation import reconcile_payment_requests
from nirantar.receivables.service import record_step
from nirantar.workflows.receivables import InvoiceStep


@dataclass
class ReceivablesDeps:
    engine: Engine
    provider: Any
    comms: Any
    environment: str = "local"


class ReceivablesActivities:
    def __init__(self, deps: ReceivablesDeps) -> None:
        self.d = deps

    @activity.defn(name="invoice_poll")
    def invoice_poll(self, step: InvoiceStep) -> dict[str, Any]:
        """Provider truth for every open request on this invoice, then its status."""
        now = datetime.fromisoformat(step.now_iso)
        reconcile_payment_requests(self.d.engine, provider_for(self.d.provider, step.tenant_id), step.tenant_id,
                                   debit_id=step.invoice_id, clock=FixedClock(now))
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            row = c.execute(text("SELECT status, amount_minor - paid_minor AS owed FROM billing.invoices WHERE "
                                 "tenant_id=:t AND invoice_id=:i"), {"t": step.tenant_id, "i": step.invoice_id}).one()
        return {"status": row.status, "owed_minor": int(row.owed)}

    @activity.defn(name="invoice_step")
    def invoice_step(self, step: InvoiceStep) -> dict[str, Any]:
        now = datetime.fromisoformat(step.now_iso)
        if step.step == "human":                          # +30 days: a person takes it from here
            with tenant_tx(step.tenant_id, self.d.engine) as c:
                inv = c.execute(text("SELECT customer_id, number, amount_minor - paid_minor AS owed FROM "
                                     "billing.invoices WHERE tenant_id=:t AND invoice_id=:i"),
                                {"t": step.tenant_id, "i": step.invoice_id}).one()
                c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, "
                               "summary, opened_at) VALUES (:t, :c, 'collections', :s, :cu, 'escalated', "
                               "CAST(:sum AS jsonb), :n) ON CONFLICT DO NOTHING"),
                          {"t": step.tenant_id, "c": new_id("cas"), "s": step.invoice_id, "cu": inv.customer_id,
                           "sum": json.dumps({"invoice": inv.number, "owed_minor": int(inv.owed),
                                              "reason": "30 days past due after the full reminder ladder"}),
                           "n": now})
                record_step(c, step.tenant_id, step.invoice_id, "human", "executed", None,
                            {"case": "collections"}, now)
            return {"status": "escalated"}
        gw = ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                         {"engine": self.d.engine, "comms": self.d.comms,
                          "provider": provider_for(self.d.provider, step.tenant_id)},
                         clock=FixedClock(now), environment=self.d.environment)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            inv = c.execute(text("SELECT customer_id, (SELECT count(*) FROM billing.invoices o WHERE "
                                 "o.tenant_id=i.tenant_id AND o.customer_id=i.customer_id AND o.status IN "
                                 "('open','partially_paid')) AS open_count FROM billing.invoices i WHERE "
                                 "i.tenant_id=:t AND i.invoice_id=:i"),
                            {"t": step.tenant_id, "i": step.invoice_id}).one()
        if step.step != "final" and int(inv.open_count) >= 2:
            # several open invoices: ONE statement per customer per day, whichever ladder gets there first
            r = gw.call(tenant_id=step.tenant_id, agent_id="receivables_agent",
                        tool_name="billing.send_invoice_statement", args={"customer_id": inv.customer_id},
                        idempotency_key=f"statement:{inv.customer_id}:{now:%Y-%m-%d}")
        else:
            tool = "billing.send_final_notice" if step.step == "final" else "billing.send_invoice_request"
            r = gw.call(tenant_id=step.tenant_id, agent_id="receivables_agent", tool_name=tool,
                        args={"invoice_id": step.invoice_id, "step": step.step},
                        idempotency_key=f"invoice:{step.invoice_id}:{step.step}")
        status = r.status
        if status == "in_progress":                       # the same statement is being sent by another ladder
            status = "bundled"
        detail = {"messages": [h.message for h in r.decision.hits] if r.decision else [], "sent": r.output.get("sent"),
                  "statement": r.output.get("invoices"),
                  "retry_after": r.decision.retry_after.isoformat() if r.decision and r.decision.retry_after else None,
                  "error": r.error or r.output.get("error"), "approval_id": r.approval_id}
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            record_step(c, step.tenant_id, step.invoice_id, step.step, status, r.action_id, detail, now)
        return {"status": status, **detail}

    def all(self) -> list[Any]:
        return [self.invoice_poll, self.invoice_step]
