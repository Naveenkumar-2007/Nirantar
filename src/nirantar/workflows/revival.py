"""RevivalWorkflow — one churned subscriber, one randomised win-back attempt, one verified outcome (P4, ADR-0014).

start (holdout: log exposure only · treated: create offer + payment link, possibly pending human approval)
→ [approval wait ≤ 2 days] → send the promotional message (Compliance Guardian: consent, window, fatigue)
→ check once a day for `window_days` → close with the verified outcome.
Reactivation is measured identically in every arm (any captured payment by the customer after the case opened),
so the holdout comparison is intention-to-treat and unbiased. Deterministic: time from workflow.now(), all I/O in
activities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACT = {"start_to_close_timeout": timedelta(seconds=90),
       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                                   maximum_interval=timedelta(minutes=2), maximum_attempts=6)}
APPROVAL_WAIT = timedelta(days=2)
APPROVAL_POLL = timedelta(hours=6)
CHECK_EVERY = timedelta(days=1)
MAX_SEND_DEFERRALS = 3


@dataclass
class RevivalInput:
    tenant_id: str
    case_id: str
    window_days: int = 30


@dataclass
class RevivalStep:
    tenant_id: str
    case_id: str
    now_iso: str
    payload: dict[str, Any]


def revival_workflow_id(tenant_id: str, case_id: str) -> str:
    return f"revival:{tenant_id}:{case_id}"


@workflow.defn
class RevivalWorkflow:
    def __init__(self) -> None:
        self.stage = "init"
        self.trace: list[dict[str, Any]] = []

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "trace": self.trace}

    async def _act(self, name: str, inp: RevivalInput, **payload: Any) -> dict[str, Any]:
        step = RevivalStep(inp.tenant_id, inp.case_id, workflow.now().isoformat(), payload)
        result: dict[str, Any] = await workflow.execute_activity(name, step, **ACT)  # type: ignore[call-overload]
        self.trace.append({"stage": self.stage, "activity": name, "at": step.now_iso, "result": result})
        return result

    @workflow.run
    async def run(self, inp: RevivalInput) -> dict[str, Any]:
        self.stage = "start"
        start = await self._act("revival_start", inp)
        if start["status"] == "pending_approval":
            self.stage = "awaiting_approval"
            waited = timedelta(0)
            while waited < APPROVAL_WAIT and not (await self._act("revival_offer_ready", inp))["ready"]:
                await workflow.sleep(APPROVAL_POLL)
                waited += APPROVAL_POLL
            start = await self._act("revival_offer_ready", inp)
            if not start["ready"]:
                self.stage = "closed"
                return await self._act("revival_close", inp, reactivated=False, reason="approval not granted in time")
        if start["status"] not in ("executed", "holdout"):     # denied / failed / invalid / needs_info
            self.stage = "closed"
            return await self._act("revival_close", inp, reactivated=False, reason=f"offer {start['status']}")
        if start["status"] != "holdout":
            self.stage = "send"
            sent = await self._act("revival_send", inp)
            for _ in range(MAX_SEND_DEFERRALS):          # outside the contact window: wait for it, then retry
                if sent["status"] == "executed" or not sent.get("retry_after"):
                    break
                self.stage = "waiting_for_contact_window"
                await workflow.sleep(max(timedelta(seconds=1),
                                         datetime.fromisoformat(sent["retry_after"]) - workflow.now()))
                self.stage = "send"
                sent = await self._act("revival_send", inp)
            if sent["status"] != "executed":
                # message withheld (e.g. consent revoked since selection): still measured, as assigned (ITT)
                self.trace.append({"stage": "send", "note": f"message {sent['status']}"})
        self.stage = "waiting"
        deadline = workflow.now() + timedelta(days=inp.window_days)
        reactivated = False
        while workflow.now() < deadline:
            await workflow.sleep(min(CHECK_EVERY, deadline - workflow.now()))
            if (await self._act("revival_check", inp))["reactivated"]:
                reactivated = True
                break
        self.stage = "closed"
        return await self._act("revival_close", inp, reactivated=reactivated,
                               reason="reactivated" if reactivated else "window ended")
