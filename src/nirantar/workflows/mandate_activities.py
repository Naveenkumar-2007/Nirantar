"""Activities for MandateSweepWorkflow and MandateRepairWorkflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.agents.mandate_doctor import MandateIn, diagnose
from nirantar.db.session import tenant_tx
from nirantar.mandates import service as mandates
from nirantar.mandates.health import scan
from nirantar.payments.providers.resolver import ProviderNotConfigured, provider_for
from nirantar.settings import service as settings
from nirantar.settings.schema import Operations
from nirantar.workflows.mandate import MandateStep, repair_workflow_id


@dataclass
class MandateDeps:
    engine: Engine
    provider: Any                       # ProviderResolver in services; a fixed provider in tests
    comms: Any
    environment: str = "local"


class MandateActivities:
    def __init__(self, deps: MandateDeps) -> None:
        self.d = deps

    def _now(self, step: MandateStep) -> datetime:
        return datetime.fromisoformat(step.now_iso)

    @activity.defn(name="mandate_scan")
    def mandate_scan(self, tenant_id: str) -> list[dict[str, Any]]:
        with tenant_tx(tenant_id, self.d.engine) as c:
            cfg = settings.model(c, tenant_id, "operations", Operations).mandate_health
            return scan(c, tenant_id, datetime.now(UTC).date(), cfg)

    @activity.defn(name="mandate_open")
    def mandate_open(self, step: MandateStep) -> dict[str, Any]:
        it, now = step.item, self._now(step)
        human = it["repair"] == "human_review" or not it["customer_action_needed"]
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            cfg = settings.model(c, step.tenant_id, "operations", Operations)
            tz = cfg.debit_cycle.timezone
            # wait for the customer until the day before the debit, at most repair_wait_days
            from zoneinfo import ZoneInfo

            day_before = datetime.combine(date.fromisoformat(it["next_debit_on"]) - timedelta(days=1), time(18),
                                          ZoneInfo(tz)).astimezone(UTC)
            deadline = min(now + timedelta(days=cfg.mandate_health.repair_wait_days), max(now, day_before))
            c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, "
                           "workflow_id, summary, opened_at) VALUES (:t, :c, 'mandate', :s, :cu, 'open', :w, "
                           "CAST(:sum AS jsonb), :n) ON CONFLICT (tenant_id, case_id) DO NOTHING"),
                      {"t": step.tenant_id, "c": step.case_id, "s": it["mandate_id"], "cu": it["customer_id"],
                       "w": repair_workflow_id(step.tenant_id, it["mandate_id"], it["problem_key"]),
                       "sum": json.dumps({**it, "deadline": deadline.isoformat()}), "n": now})
        return {"case_id": step.case_id, "action": "human" if human else "contact",
                "deadline": deadline.isoformat()}

    @activity.defn(name="mandate_send")
    def mandate_send(self, step: MandateStep) -> dict[str, Any]:
        from nirantar.core.clock import FixedClock
        from nirantar.mcp.gateway import ToolGateway
        from nirantar.mcp.tools import AGENT_SCOPES, TOOLS

        gw = ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                         {"engine": self.d.engine, "comms": self.d.comms,
                          "provider": provider_for(self.d.provider, step.tenant_id)},
                         clock=FixedClock(self._now(step)), environment=self.d.environment)
        r = gw.call(tenant_id=step.tenant_id, agent_id="mandate_doctor", tool_name="mandate.send_repair",
                    args={"case_id": step.case_id}, case_id=step.case_id)
        return {"status": r.status, "action_id": r.action_id, "provider_ref": r.output.get("provider_ref"),
                "error": r.output.get("error")}

    @activity.defn(name="mandate_check")
    def mandate_check(self, step: MandateStep) -> dict[str, Any]:
        """Provider truth: does the customer now hold an active mandate that covers the next debit?"""
        it, now = step.item, self._now(step)
        try:
            provider = provider_for(self.d.provider, step.tenant_id)
        except ProviderNotConfigured as exc:
            return {"healthy": False, "reason": str(exc)[:200]}
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            ref: str | None = c.execute(
                text("SELECT provider_customer_ref FROM billing.mandates WHERE tenant_id=:t AND mandate_id=:m"),
                {"t": step.tenant_id, "m": it["mandate_id"]}).scalar_one()
        lister = getattr(provider, "list_mandates", None)
        if not ref or lister is None:
            return {"healthy": False, "reason": "provider cannot list mandates"}
        due, amount = date.fromisoformat(it["next_debit_on"]), int(it["next_amount_minor"])
        # "healthy" = the SAME Mandate Doctor rules that flagged the problem now find nothing to repair
        good = [m for m in lister(ref) if diagnose(MandateIn(
            mandate_id=m.provider_token_id, rail=m.rail, status=m.status,
            valid_until=m.valid_until.date() if m.valid_until else None,
            max_amount_minor=m.max_amount.minor if m.max_amount else None, next_debit_amount_minor=amount,
            next_debit_on=due, today=now.date())).repair == "none"]
        if not good:
            return {"healthy": False}
        best = max(good, key=lambda m: (m.valid_until or datetime.max.replace(tzinfo=UTC)))
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            change = mandates.upsert_mandate(c, step.tenant_id, best, it["customer_id"], now)
            c.execute(text("UPDATE billing.subscriptions SET mandate_id=:m WHERE tenant_id=:t AND subscription_id=:s"),
                      {"m": change.mandate_id, "t": step.tenant_id, "s": it["subscription_id"]})
        return {"healthy": True, "mandate_id": change.mandate_id}

    @activity.defn(name="mandate_escalate")
    def mandate_escalate(self, step: MandateStep) -> dict[str, Any]:
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            c.execute(text("UPDATE ops.cases SET status='escalated', summary = summary || CAST(:s AS jsonb) WHERE "
                           "tenant_id=:t AND case_id=:c"),
                      {"s": json.dumps({"escalation": step.payload.get("reason")}), "t": step.tenant_id,
                       "c": step.case_id})
        return {"escalated": True}

    @activity.defn(name="mandate_close")
    def mandate_close(self, step: MandateStep) -> dict[str, Any]:
        from nirantar.core.ids import new_id

        outcome, now = str(step.payload["outcome"]), self._now(step)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            if outcome in ("repaired", "unrepaired"):
                c.execute(text("INSERT INTO ai.labels (tenant_id, label_id, prediction_id, subject_id, label_name, "
                               "value, source, observed_at) VALUES (:t, :l, NULL, :s, 'mandate_repaired', "
                               "CAST(:v AS jsonb), 'provider', :n)"),
                          {"t": step.tenant_id, "l": new_id("lbl"), "s": step.item["mandate_id"],
                           "v": json.dumps({"value": outcome == "repaired", "repair": step.item["repair"]}), "n": now})
            c.execute(text("UPDATE ops.cases SET status=CASE WHEN :o='escalated' THEN 'escalated' ELSE 'closed' END, "
                           "closed_at=CASE WHEN :o='escalated' THEN NULL ELSE CAST(:n AS timestamptz) END, "
                           "summary = summary || CAST(:s AS jsonb) WHERE tenant_id=:t AND case_id=:c"),
                      {"o": outcome, "n": now, "s": json.dumps({"outcome": outcome,
                                                                "repaired_by": step.payload.get("mandate_id")}),
                       "t": step.tenant_id, "c": step.case_id})
        return {"outcome": outcome}

    def all(self) -> list[Any]:
        return [self.mandate_scan, self.mandate_open, self.mandate_send, self.mandate_check, self.mandate_escalate,
                self.mandate_close]
