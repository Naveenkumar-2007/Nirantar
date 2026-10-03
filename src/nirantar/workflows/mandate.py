"""Mandate health (P5, ADR-0015): a daily sweep finds mandates that will break the next debit; each problem gets one
MandateRepairWorkflow.

MandateSweepWorkflow   every tenant with a provider → scan (Mandate Doctor rules on verified mandates) → start one
                       repair workflow per problem (deterministic id: the same problem is never worked twice).
MandateRepairWorkflow  open case → fraud-revoked or no customer action possible → human; else send the repair request
                       through the MCP gateway (re-authorisation link, or "resume in your UPI app") → re-check with
                       the provider daily (a re-registration creates a NEW token, linked to the subscription when it
                       appears) and on `mandate_update` signals → close as repaired / unrepaired with a provider label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=2),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=6)}
CHECK_EVERY = timedelta(days=1)
SEND_RETRY_AFTER = timedelta(hours=12)
MAX_SEND_ATTEMPTS = 3


@dataclass
class MandateSweepInput:
    tenants: list[str] = field(default_factory=list)     # empty = every active tenant with a provider


@dataclass
class MandateRepairInput:
    tenant_id: str
    item: dict[str, Any]


@dataclass
class MandateStep:
    tenant_id: str
    case_id: str
    now_iso: str
    item: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)


def repair_workflow_id(tenant_id: str, mandate_id: str, problem_key: str) -> str:
    return f"mandate:{tenant_id}:{mandate_id}:{problem_key}"


def repair_case_id(problem_key: str, mandate_id: str) -> str:
    return f"cas_md{mandate_id[-10:]}{problem_key[:10]}"


@workflow.defn
class MandateSweepWorkflow:
    @workflow.run
    async def run(self, inp: MandateSweepInput) -> dict[str, Any]:
        tenants = inp.tenants or await workflow.execute_activity("sweep_list_tenants", inp, **ACT)
        started = existing = 0
        for t in tenants:
            items: list[dict[str, Any]] = await workflow.execute_activity("mandate_scan", t, **ACT)
            for it in items:
                try:
                    await workflow.start_child_workflow(
                        MandateRepairWorkflow.run, MandateRepairInput(t, it),
                        id=repair_workflow_id(t, it["mandate_id"], it["problem_key"]),
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON)
                    started += 1
                except WorkflowAlreadyStartedError:
                    existing += 1
        return {"tenants": len(tenants), "started": started, "already_handled": existing}


@workflow.defn
class MandateRepairWorkflow:
    def __init__(self) -> None:
        self.stage = "init"
        self._wake = False
        self.trace: list[dict[str, Any]] = []

    @workflow.signal
    def mandate_update(self, update: dict[str, Any]) -> None:
        self._wake = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "trace": self.trace}

    async def _act(self, name: str, inp: MandateRepairInput, case_id: str, **payload: Any) -> dict[str, Any]:
        step = MandateStep(inp.tenant_id, case_id, workflow.now().isoformat(), inp.item, payload)
        out: dict[str, Any] = await workflow.execute_activity(name, step, **ACT)
        self.trace.append({"stage": self.stage, "activity": name, "at": step.now_iso, "result": out})
        return out

    @workflow.run
    async def run(self, inp: MandateRepairInput) -> dict[str, Any]:
        case_id = repair_case_id(inp.item["problem_key"], inp.item["mandate_id"])
        self.stage = "open"
        opened = await self._act("mandate_open", inp, case_id)
        if opened["action"] == "human":
            self.stage = "escalated"
            await self._act("mandate_escalate", inp, case_id, reason=inp.item["rationale"])
            return await self._act("mandate_close", inp, case_id, outcome="escalated")

        self.stage = "contact"
        sent: dict[str, Any] = {}
        for attempt in range(MAX_SEND_ATTEMPTS):
            sent = await self._act("mandate_send", inp, case_id)
            if sent["status"] == "executed":
                break
            if attempt + 1 < MAX_SEND_ATTEMPTS:          # contact window / transient channel issue: try later
                await workflow.sleep(SEND_RETRY_AFTER)
        if sent.get("status") != "executed":
            self.stage = "escalated"
            await self._act("mandate_escalate", inp, case_id, reason=f"repair request not sent: {sent.get('status')}")
            return await self._act("mandate_close", inp, case_id, outcome="escalated")

        self.stage = "waiting_customer"
        deadline = datetime.fromisoformat(opened["deadline"])
        while True:
            check = await self._act("mandate_check", inp, case_id)
            if check["healthy"]:
                self.stage = "closed"
                return await self._act("mandate_close", inp, case_id, outcome="repaired",
                                       mandate_id=check.get("mandate_id"))
            remaining = deadline - workflow.now()
            if remaining <= timedelta(0):
                break
            self._wake = False
            try:
                await workflow.wait_condition(lambda: self._wake, timeout=min(CHECK_EVERY, remaining))
            except TimeoutError:
                pass
        self.stage = "closed"
        return await self._act("mandate_close", inp, case_id, outcome="unrepaired")
