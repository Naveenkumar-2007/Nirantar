"""DebitCycleWorkflow — one scheduled debit from T-3 to a verified outcome (BB-§13).

Stages: open case → M1 risk → pre-debit plan + mandatory notice → sleep until debit → await payment
signal (reconcile if none arrives) → [failed] Conductor recovery rounds, waiting for contact windows,
customer replies and payment → verify → close with labels and experiment outcome.

Deterministic by construction: no I/O here; time comes from workflow.now(); all side effects are
activities with retries. Signals are idempotent (a late 'failed' never overrides 'captured').
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nirantar.workflows.types import CycleInput, StepInput

ACT = {"start_to_close_timeout": timedelta(seconds=90),
       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                                   maximum_interval=timedelta(minutes=2), maximum_attempts=6)}


@workflow.defn
class DebitCycleWorkflow:
    def __init__(self) -> None:
        self._payment: dict[str, Any] | None = None
        self._captured = False
        self._replies: list[dict[str, Any]] = []
        self._handled_replies = 0
        self.stage = "init"
        self.trace: list[dict[str, Any]] = []

    # ---- signals / queries ------------------------------------------------------
    @workflow.signal
    def payment_update(self, event: dict[str, Any]) -> None:
        if self._captured:
            return  # payment state only moves forward
        self._payment = event
        if event.get("event_type") == "payment.captured":
            self._captured = True

    @workflow.signal
    def customer_reply(self, reply: dict[str, Any]) -> None:
        self._replies.append(reply)

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "captured": self._captured, "replies": len(self._replies),
                "handled_replies": self._handled_replies, "trace": self.trace}

    # ---- helpers ------------------------------------------------------------------
    def _step(self, inp: CycleInput, case_id: str, **payload: Any) -> StepInput:
        return StepInput(inp, workflow.now().isoformat(), case_id, payload)

    async def _act(self, name: str, step: StepInput) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(name, step, **ACT)  # type: ignore[call-overload]
        self.trace.append({"stage": self.stage, "activity": name, "at": step.now_iso, "result": result})
        return result

    async def _drain_replies(self, inp: CycleInput, case_id: str) -> None:
        while self._handled_replies < len(self._replies):
            reply = self._replies[self._handled_replies]
            await self._act("record_reply", self._step(inp, case_id, **reply))
            self._handled_replies += 1

    async def _wait(self, until: datetime, inp: CycleInput, case_id: str) -> None:
        """Wait until `until`, waking early for payment or replies (replies are processed immediately)."""
        while not self._captured:
            remaining = until - workflow.now()
            if remaining <= timedelta(0):
                return
            try:
                await workflow.wait_condition(
                    lambda: self._captured or self._handled_replies < len(self._replies), timeout=remaining)
            except TimeoutError:
                return
            await self._drain_replies(inp, case_id)

    # ---- run ------------------------------------------------------------------------
    @workflow.run
    async def run(self, inp: CycleInput) -> dict[str, Any]:
        case_id = f"cas_{inp.debit_id.split('_', 1)[1]}"
        self.stage = "open"
        await self._act("open_case", self._step(inp, case_id))
        self.stage = "predict"
        risk = await self._act("predict_risk", self._step(inp, case_id))
        debit_at = datetime.fromisoformat(inp.debit_at_iso)
        if workflow.now() < debit_at:
            self.stage = "pre_debit"
            await self._act("pre_debit", self._step(inp, case_id, p_fail=risk.get("p_fail"),
                                                    cash_day_distance=risk.get("cash_day_distance")))
        else:   # started late (a payment event arrived first): a "pre"-debit notice now would be misleading
            self.trace.append({"stage": "pre_debit", "activity": None, "at": workflow.now().isoformat(),
                               "result": {"skipped": "debit time already passed"}})

        self.stage = "waiting_until_debit"
        if debit_at > workflow.now():
            await workflow.sleep(debit_at - workflow.now())
        await self._act("mark_attempting", self._step(inp, case_id))

        self.stage = "awaiting_payment"
        try:
            await workflow.wait_condition(lambda: self._payment is not None,
                                          timeout=timedelta(hours=inp.payment_wait_hours))
        except TimeoutError:
            self.stage = "reconciling"
            recon = await self._act("reconcile", self._step(inp, case_id))
            if recon.get("payment_event"):
                self.payment_update(recon["payment_event"])

        if self._captured:
            outcome = "paid_on_time"
        else:
            failure = self._payment or {"event_type": "payment.unknown", "error_code": None, "error_reason": None}
            deadline = workflow.now() + timedelta(days=inp.recovery_window_days)
            rounds = 0
            while not self._captured and workflow.now() < deadline and rounds < inp.max_contact_rounds:
                rounds += 1
                self.stage = "recovery"
                res = await self._act("handle_failure", self._step(
                    inp, case_id, error_code=failure.get("error_code"), error_reason=failure.get("error_reason"),
                    attempt=rounds))
                if not res.get("chosen_arm") and res.get("retry_after"):
                    # Every channel is closed right now (e.g. night): wait for the window, then retry this round.
                    self.stage = "waiting_contact_window"
                    await self._wait(min(datetime.fromisoformat(res["retry_after"]), deadline), inp, case_id)
                    rounds -= 1
                    continue
                self.stage = "awaiting_recovery"
                await self._wait(min(deadline, workflow.now() + timedelta(days=inp.round_gap_days)), inp, case_id)
            if not self._captured and workflow.now() < deadline:
                self.stage = "awaiting_recovery"
                await self._wait(deadline, inp, case_id)
            if not self._captured:
                self.stage = "reconciling"
                recon = await self._act("reconcile", self._step(inp, case_id))
                if recon.get("payment_event"):
                    self.payment_update(recon["payment_event"])
            outcome = "recovered" if self._captured else "unrecovered"

        await self._drain_replies(inp, case_id)
        self.stage = "verifying"
        closed = await self._act("verify_and_close", self._step(inp, case_id, expect=outcome,
                                                                prediction_id=risk.get("prediction_id")))
        self.stage = "closed"
        return {"case_id": case_id, "outcome": closed["outcome"], "verified": closed["verified"],
                "risk": risk, "trace": self.trace}
