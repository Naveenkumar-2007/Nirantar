"""DisputeWorkflow — one chargeback from `dispute.opened` to a provider outcome (P5, ADR-0015).

prepare: case + verified evidence items + win probability (transparent M11 prior) + representment draft + evidence
pack PDF in the object store → decide: contest when the evidence supports the merchant and the deadline allows
(else escalate to a human — never auto-accept: accepting is always a human decision) → submit through the MCP
gateway (Documents API upload + contest; maker-checker above threshold) → wait for `dispute_update` signals from
the event bridge (won / lost / accepted) → close with a provider-sourced label for M11.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACT = {"start_to_close_timeout": timedelta(seconds=120),
       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                                   maximum_interval=timedelta(minutes=2), maximum_attempts=6)}
APPROVAL_POLL = timedelta(hours=6)
OUTCOME_WAIT = timedelta(days=90)


@dataclass
class DisputeInput:
    tenant_id: str
    dispute_id: str


@dataclass
class DisputeStep:
    tenant_id: str
    dispute_id: str
    now_iso: str
    payload: dict[str, Any]


def dispute_workflow_id(tenant_id: str, dispute_id: str) -> str:
    return f"dispute:{tenant_id}:{dispute_id}"


def dispute_case_id(dispute_id: str) -> str:
    return "cas_dp" + hashlib.sha256(dispute_id.encode()).hexdigest()[:20]


@workflow.defn
class DisputeWorkflow:
    def __init__(self) -> None:
        self.stage = "init"
        self.outcome: str | None = None
        self.trace: list[dict[str, Any]] = []

    @workflow.signal
    def dispute_update(self, update: dict[str, Any]) -> None:
        if update.get("status") in ("won", "lost", "accepted"):
            self.outcome = str(update["status"])

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "outcome": self.outcome, "trace": self.trace}

    async def _act(self, name: str, inp: DisputeInput, **payload: Any) -> dict[str, Any]:
        step = DisputeStep(inp.tenant_id, inp.dispute_id, workflow.now().isoformat(), payload)
        result: dict[str, Any] = await workflow.execute_activity(name, step, **ACT)  # type: ignore[call-overload]
        self.trace.append({"stage": self.stage, "activity": name, "at": step.now_iso, "result": result})
        return result

    @workflow.run
    async def run(self, inp: DisputeInput) -> dict[str, Any]:
        self.stage = "prepare"
        prep = await self._act("dispute_prepare", inp)
        respond_by = datetime.fromisoformat(prep["respond_by"])
        if prep["decision"] == "contest":
            self.stage = "submit"
            sub = await self._act("dispute_submit", inp)
            while sub["status"] == "pending_approval" and workflow.now() + APPROVAL_POLL < respond_by:
                self.stage = "awaiting_approval"
                await workflow.sleep(APPROVAL_POLL)
                sub = await self._act("dispute_submitted", inp)
            if sub["status"] not in ("executed", "submitted"):
                self.stage = "escalated"
                await self._act("dispute_escalate", inp, reason=f"submission {sub['status']}")
        else:
            self.stage = "escalated"
            await self._act("dispute_escalate", inp, reason=prep["reason"])
        self.stage = "waiting_outcome"
        try:
            await workflow.wait_condition(lambda: self.outcome is not None, timeout=OUTCOME_WAIT)
        except TimeoutError:
            pass
        self.stage = "closed"
        return await self._act("dispute_close", inp, outcome=self.outcome or "unknown")
