"""PaymentHealthWorkflow (P10, ADR-0024): every 15 minutes — detect issuer incidents, then recover what they broke.

scan (platform-wide aggregates) → for every incident that has closed and is not yet recovered, for every business
with affected debits: one honest "the bank had a problem, it's fixed" message with a fresh payment link, through the
gateway (consent, window, fatigue, audit), idempotent per incident and debit → mark the incident recovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nirantar.workflows.platform import SweepInput

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=5),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=5)}


@dataclass
class HealthInput:
    tenants: list[str] = field(default_factory=list)       # empty = every active business with a provider


@dataclass
class HealthStep:
    now_iso: str
    tenants: list[str] = field(default_factory=list)
    incident_id: str = ""
    tenant_id: str = ""


@workflow.defn
class PaymentHealthWorkflow:
    @workflow.run
    async def run(self, inp: HealthInput) -> dict[str, Any]:
        tenants = inp.tenants or await workflow.execute_activity("sweep_list_tenants", SweepInput(), **ACT)
        now = workflow.now().isoformat()
        scanned: dict[str, Any] = await workflow.execute_activity("health_scan", HealthStep(now, tenants), **ACT)
        recovered = 0
        for iid in await workflow.execute_activity("health_pending_recoveries", HealthStep(now), **ACT):
            for t in tenants:
                r: dict[str, Any] = await workflow.execute_activity(
                    "health_recover", HealthStep(workflow.now().isoformat(), [], iid, t), **ACT)
                recovered += int(r["sent"])
            await workflow.execute_activity("health_mark_recovered", HealthStep(now, [], iid), **ACT)
        return {"opened": len(scanned["opened"]), "closed": len(scanned["closed"]), "messages": recovered}


@dataclass
class HealthDeps:
    engine: Engine
    owner: Engine
    provider: Any
    comms: Any
    environment: str = "local"


class HealthActivities:
    def __init__(self, deps: HealthDeps) -> None:
        self.d = deps

    @activity.defn(name="health_scan")
    def health_scan(self, step: HealthStep) -> dict[str, Any]:
        from nirantar.health.monitor import scan

        out = scan(self.d.engine, self.d.owner, step.tenants, datetime.fromisoformat(step.now_iso))
        return {"series": out["series"], "opened": out["opened"], "closed": out["closed"]}

    @activity.defn(name="health_pending_recoveries")
    def health_pending_recoveries(self, step: HealthStep) -> list[str]:
        with self.d.owner.connect() as c:
            return [r[0] for r in c.execute(text(
                "SELECT incident_id FROM core.payment_incidents WHERE status='closed' AND NOT recovery_done "
                "ORDER BY ended_at"))]

    @activity.defn(name="health_recover")
    def health_recover(self, step: HealthStep) -> dict[str, Any]:
        from nirantar.core.clock import FixedClock
        from nirantar.health.monitor import affected_debits, record_recovery
        from nirantar.mcp.gateway import ToolGateway
        from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
        from nirantar.payments.providers.resolver import ProviderNotConfigured, provider_for

        now = datetime.fromisoformat(step.now_iso)
        with self.d.owner.connect() as c:
            incident = c.execute(text("SELECT * FROM core.payment_incidents WHERE incident_id=:i"),
                                 {"i": step.incident_id}).one()
        debits = affected_debits(self.d.engine, step.tenant_id, incident)
        if not debits:
            return {"sent": 0, "affected": 0}
        try:
            provider = provider_for(self.d.provider, step.tenant_id)
        except ProviderNotConfigured:
            return {"sent": 0, "affected": len(debits), "error": "no provider"}
        gw = ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                         {"engine": self.d.engine, "comms": self.d.comms, "provider": provider},
                         clock=FixedClock(now), environment=self.d.environment)
        sent = 0
        for x in debits:
            r = gw.call(tenant_id=step.tenant_id, agent_id="billing_agent", tool_name="billing.send_payment_request",
                        args={"debit_id": x["debit_id"], "occasion": "incident"},
                        idempotency_key=f"incident:{step.incident_id}:{x['debit_id']}")
            detail = {"action_id": r.action_id, "messages": [h.message for h in r.decision.hits] if r.decision else [],
                      "error": r.error or r.output.get("error"), "sent": r.output.get("sent")}
            record_recovery(self.d.engine, step.tenant_id, step.incident_id, x["debit_id"], x["customer_id"],
                            r.status, detail, now)
            sent += int(r.status == "executed" and bool(r.output.get("sent")))
        return {"sent": sent, "affected": len(debits)}

    @activity.defn(name="health_mark_recovered")
    def health_mark_recovered(self, step: HealthStep) -> None:
        with self.d.owner.begin() as c:
            c.execute(text("UPDATE core.payment_incidents SET recovery_done=true WHERE incident_id=:i"),
                      {"i": step.incident_id})

    def all(self) -> list[Any]:
        return [self.health_scan, self.health_pending_recoveries, self.health_recover, self.health_mark_recovered]
