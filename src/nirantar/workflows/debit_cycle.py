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

PAY_BY_LINK = "pay-by-link-collection-v1"          # P8.6: patched in; replay-safe for histories recorded before
PROMISES = "promise-to-pay-v1"                       # P10: honour a customer's promised date
PROMISE_MAX = timedelta(days=30)
LINK_POLL = timedelta(minutes=30)
COLLECT_TRIES = 3

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
        self._by_link = False                 # set only on the patched path (replay-safe)
        self._promise: str | None = None      # ISO date the customer promised to pay (patched path only)
        self._promise_polling = False
        self._honouring: str | None = None    # the promise currently being honoured

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
            res = await self._act("record_reply", self._step(inp, case_id, **reply))
            self._handled_replies += 1
            if res.get("promised_date") and workflow.patched(PROMISES):
                self._promise = str(res["promised_date"])

    async def _wait(self, until: datetime, inp: CycleInput, case_id: str) -> None:
        """Wait until `until`, waking early for payment or replies (replies are processed immediately)."""
        while not self._captured:
            remaining = until - workflow.now()
            if remaining <= timedelta(0):
                return
            poll = (self._by_link or self._promise_polling) and remaining > LINK_POLL  # no charge event by itself
            try:
                await workflow.wait_condition(
                    lambda: self._captured or self._handled_replies < len(self._replies),
                    timeout=LINK_POLL if poll else remaining)
            except TimeoutError:
                if not poll:
                    return
                polled = await self._act("poll_link", self._step(inp, case_id))
                if polled.get("payment_event"):
                    self.payment_update(polled["payment_event"])
                continue
            await self._drain_replies(inp, case_id)
            if self._promise is not None and self._promise != self._honouring:
                return                                     # a new promise takes over at once (patched path only)

    async def _honour_promise(self, inp: CycleInput, case_id: str, deadline: datetime) -> datetime:
        """No chasing until the promised day; that morning a reminder with a link; kept or broken by the end of the
        day (provider-verified). A promise can extend the recovery window, never beyond PROMISE_MAX from now."""
        promised = self._promise or ""
        y, m, d = (int(x) for x in promised[:10].split("-"))
        start = datetime(y, m, d, 4, 0, tzinfo=workflow.now().tzinfo)     # 04:00 UTC ≈ 09:30 IST: window open
        end = start + timedelta(days=1)
        if end - workflow.now() > PROMISE_MAX:
            self._promise = None                                         # too far out: not honoured as a pause
            return deadline
        deadline = max(deadline, end)
        self.stage = "promised"
        self._promise_polling = True
        self._honouring = promised
        if workflow.now() < start:
            await self._wait(start, inp, case_id)
        if self._promise == promised and not self._captured:
            await self._act("promise_remind", self._step(inp, case_id, promised_date=promised))
            await self._wait(end, inp, case_id)
        if self._promise == promised:                                    # not superseded by a newer promise
            await self._act("promise_resolve", self._step(inp, case_id))
            self._promise = None
        self._promise_polling = False
        self._honouring = None
        return deadline

    async def _collect(self, inp: CycleInput, case_id: str) -> bool:
        """Due-date collection. True when Nirantar collects this debit by payment link."""
        self.stage = "collecting"
        for _ in range(COLLECT_TRIES):
            res = await self._act("collect", self._step(inp, case_id))
            if res.get("method") != "payment_link":
                return False
            if res.get("status") == "denied" and res.get("retry_after"):
                self.stage = "waiting_contact_window"      # e.g. due at night: send when the window opens
                await self._wait(datetime.fromisoformat(res["retry_after"]), inp, case_id)
                if self._captured:
                    return True
                continue
            return True
        return True

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

        by_link = False
        if workflow.patched(PAY_BY_LINK):
            by_link = self._by_link = await self._collect(inp, case_id)

        self.stage = "awaiting_payment"
        if by_link:
            # no provider charge will arrive by itself: poll the link until paid or the wait is over (a webhook, when
            # one is configured, signals earlier and ends the wait at once)
            until = workflow.now() + timedelta(hours=inp.payment_wait_hours)
            while not self._captured and workflow.now() < until:
                try:
                    await workflow.wait_condition(lambda: self._captured,
                                                  timeout=min(LINK_POLL, until - workflow.now()))
                except TimeoutError:
                    polled = await self._act("poll_link", self._step(inp, case_id))
                    if polled.get("payment_event"):
                        self.payment_update(polled["payment_event"])
            if not self._captured:
                self._payment = {"event_type": "payment.not_paid", "error_code": "NOT_PAID",
                                 "error_reason": "not_paid_by_due_date"}
        else:
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
            while not self._captured and workflow.now() < deadline:
                if self._promise is not None:
                    deadline = await self._honour_promise(inp, case_id, deadline)
                    continue
                if rounds >= inp.max_contact_rounds:
                    break
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
