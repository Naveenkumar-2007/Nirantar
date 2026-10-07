"""InvoiceChaseWorkflow (P10, ADR-0025): one invoice, from reminder to paid — or to a person.

For each ladder step (reminder −3 · due 0 · overdue_1 +3 · overdue_2 +7 · final +14 · human +30, days from due):
wait until 10:00 IST that day, checking the provider every 6 hours (and waking at once on an `invoice_update`
signal: payment webhook, dispute, write-off) → if the invoice is closed or disputed, stop → otherwise run the step
through the gateway. The final notice always waits for a person's approval; "human" opens a collections case.
Deterministic: no I/O here; every side effect is an activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=2),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=6)}
POLL = timedelta(hours=6)
DEFER_DENIED = "invoice-defer-denied-step"      # replay-safe: histories before this change never deferred
MAX_DEFERRALS = 2
LADDER: tuple[tuple[str, int], ...] = (("reminder", -3), ("due", 0), ("overdue_1", 3), ("overdue_2", 7),
                                       ("final", 14), ("human", 30))


@dataclass
class InvoiceInput:
    tenant_id: str
    invoice_id: str
    due_on: str                     # ISO date


@dataclass
class InvoiceStep:
    tenant_id: str
    invoice_id: str
    now_iso: str
    step: str = ""


def invoice_workflow_id(tenant_id: str, invoice_id: str) -> str:
    return f"invoice:{tenant_id}:{invoice_id}"


@workflow.defn
class InvoiceChaseWorkflow:
    def __init__(self) -> None:
        self._wake = False
        self.stage = "init"
        self.trace: list[dict[str, Any]] = []

    @workflow.signal
    def invoice_update(self, _: dict[str, Any]) -> None:
        self._wake = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "trace": self.trace}

    def _step(self, inp: InvoiceInput, step: str = "") -> InvoiceStep:
        return InvoiceStep(inp.tenant_id, inp.invoice_id, workflow.now().isoformat(), step)

    async def _state(self, inp: InvoiceInput) -> str:
        r: dict[str, Any] = await workflow.execute_activity("invoice_poll", self._step(inp), **ACT)
        return str(r["status"])

    async def _wait_until(self, inp: InvoiceInput, at: datetime) -> str | None:
        """Sleep until `at`, checking the provider every 6 hours or on a signal; the closing state if it closed."""
        while workflow.now() < at:
            self._wake = False
            try:
                await workflow.wait_condition(lambda: self._wake, timeout=min(POLL, at - workflow.now()))
            except TimeoutError:
                pass
            state = await self._state(inp)
            if state not in ("open", "partially_paid"):
                return state
        return None

    @workflow.run
    async def run(self, inp: InvoiceInput) -> dict[str, Any]:
        due = date.fromisoformat(inp.due_on)
        tz = workflow.now().tzinfo
        for step, offset in LADDER:
            day = due + timedelta(days=offset)
            self.stage = f"waiting:{step}"
            closed = await self._wait_until(inp, datetime(day.year, day.month, day.day, 4, 30, tzinfo=tz))  # 10:00 IST
            for attempt in range(MAX_DEFERRALS + 1):
                state = closed or await self._state(inp)
                if state not in ("open", "partially_paid"):
                    self.stage = state
                    return {"outcome": state, "trace": self.trace}
                self.stage = step
                res: dict[str, Any] = await workflow.execute_activity("invoice_step", self._step(inp, step), **ACT)
                self.trace.append({"step": step, "at": workflow.now().isoformat(), "result": res})
                # A step that came due outside the contact window (an invoice issued at 11 pm, a worker that was
                # down) is deferred to when policy allows it, not lost.
                retry_after = res.get("retry_after")
                if (res.get("status") != "denied" or not retry_after or attempt == MAX_DEFERRALS
                        or not workflow.patched(DEFER_DENIED)):
                    break
                self.stage = f"deferred:{step}"
                closed = await self._wait_until(inp, datetime.fromisoformat(retry_after))
            if step == "human":
                break
        self.stage = "with_a_person"
        return {"outcome": "escalated", "trace": self.trace}
