"""CheckoutRecoveryWorkflow (ADR-0028): one checkout, from "the customer went quiet" to paid, expired or stopped.

1. Wait until the checkout has been quiet for ABANDON_AFTER (any new event — a page view, a failed attempt, a
   payment — restarts the wait; a payment ends the workflow).
2. Assess once: diagnose the cause, check eligibility (a customer to contact, above the minimum value), assign the
   experiment arm. Holdout and ineligible checkouts are only observed — that is what makes the uplift measurable.
3. Nudge (a reminder with a fresh link for exactly the checkout's amount, worded for the cause), then at most ONE
   follow-up FOLLOW_UP_AFTER later. A step refused for the contact window is deferred to when policy allows it (at
   most MAX_DEFERRALS times); a step refused for consent, opt-out or fatigue ends the contact for this checkout.
4. High-value checkouts still open after the follow-up go to a person (a case), never to more messages.
5. At EXPIRE_AFTER from creation an open checkout is closed as expired.
Deterministic: no I/O here; every side effect is an activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=2),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=6)}
ABANDON_AFTER = timedelta(minutes=30)
FOLLOW_UP_AFTER = timedelta(hours=20)
ESCALATE_AFTER = timedelta(hours=4)          # after the follow-up, for high-value checkouts only
EXPIRE_AFTER = timedelta(hours=72)
POLL = timedelta(hours=6)
MAX_DEFERRALS = 2


@dataclass
class CheckoutInput:
    tenant_id: str
    session_id: str


@dataclass
class CheckoutStep:
    tenant_id: str
    session_id: str
    now_iso: str
    step: str = ""


def checkout_workflow_id(tenant_id: str, session_id: str) -> str:
    return f"checkout:{tenant_id}:{session_id}"


@workflow.defn
class CheckoutRecoveryWorkflow:
    def __init__(self) -> None:
        self._wake = False
        self.stage = "init"
        self.trace: list[dict[str, Any]] = []

    @workflow.signal
    def checkout_update(self, _: dict[str, Any]) -> None:
        self._wake = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "trace": self.trace}

    def _step(self, inp: CheckoutInput, step: str = "") -> CheckoutStep:
        return CheckoutStep(inp.tenant_id, inp.session_id, workflow.now().isoformat(), step)

    async def _poll(self, inp: CheckoutInput) -> dict[str, Any]:
        r: dict[str, Any] = await workflow.execute_activity("checkout_poll", self._step(inp), **ACT)
        return r

    async def _sleep(self, until: datetime) -> None:
        self._wake = False
        try:
            await workflow.wait_condition(lambda: self._wake, timeout=max(timedelta(seconds=1),
                                                                         min(POLL, until - workflow.now())))
        except TimeoutError:
            pass

    async def _wait_until(self, inp: CheckoutInput, at: datetime) -> str | None:
        """Sleep until `at`, waking on any checkout event; the closing status if the checkout closed meanwhile."""
        while workflow.now() < at:
            await self._sleep(at)
            st = await self._poll(inp)
            if st["status"] != "open":
                return str(st["status"])
        return None

    def _done(self, outcome: str) -> dict[str, Any]:
        self.stage = outcome
        return {"outcome": outcome, "trace": self.trace}

    async def _run_step(self, inp: CheckoutInput, step: str) -> dict[str, Any]:
        """One contact step, deferred (not dropped) when the contact window refuses it."""
        res: dict[str, Any] = {}
        for attempt in range(MAX_DEFERRALS + 1):
            self.stage = step
            res = await workflow.execute_activity("checkout_step", self._step(inp, step), **ACT)
            self.trace.append({"step": step, "at": workflow.now().isoformat(), "result": res})
            retry_after = res.get("retry_after")
            if res.get("status") != "denied" or not retry_after or attempt == MAX_DEFERRALS:
                return res
            self.stage = f"deferred:{step}"
            closed = await self._wait_until(inp, datetime.fromisoformat(retry_after))
            if closed:
                return {"status": "closed", "closed": closed}
        return res

    @workflow.run
    async def run(self, inp: CheckoutInput) -> dict[str, Any]:
        # 1. quiet period: the customer may still be paying
        while True:
            st = await self._poll(inp)
            if st["status"] != "open":
                return self._done(str(st["status"]))
            quiet = datetime.fromisoformat(st["last_activity_at"]) + ABANDON_AFTER
            if workflow.now() >= quiet:
                break
            self.stage = "waiting:quiet"
            await self._sleep(quiet)
        expire_at = datetime.fromisoformat(st["created_at"]) + EXPIRE_AFTER

        # 2. assess once
        a: dict[str, Any] = await workflow.execute_activity("checkout_assess", self._step(inp), **ACT)
        self.trace.append({"step": "assess", "at": workflow.now().isoformat(), "result": a})

        contacted = False
        if a["eligible"] and a["arm"] == "treatment":
            # 3. nudge, then at most one follow-up
            res = await self._run_step(inp, "nudge")
            if res.get("status") == "closed":
                return self._done(str(res["closed"]))
            contacted = res.get("status") == "executed" and bool(res.get("sent"))
            if contacted:
                self.stage = "waiting:follow_up"
                closed = await self._wait_until(inp, workflow.now() + FOLLOW_UP_AFTER)
                if closed:
                    return self._done(closed)
                res = await self._run_step(inp, "follow_up")
                if res.get("status") == "closed":
                    return self._done(str(res["closed"]))
                # 4. high value: a person, not more messages
                if a.get("escalate"):
                    self.stage = "waiting:escalate"
                    closed = await self._wait_until(inp, workflow.now() + ESCALATE_AFTER)
                    if closed:
                        return self._done(closed)
                    r = await workflow.execute_activity("checkout_step", self._step(inp, "escalate"), **ACT)
                    self.trace.append({"step": "escalate", "at": workflow.now().isoformat(), "result": r})
        else:
            await workflow.execute_activity("checkout_step", self._step(
                inp, "held_out" if a["eligible"] else "ineligible"), **ACT)

        # 5. observe until expiry (holdout outcomes are what the uplift is measured against)
        self.stage = "observing"
        closed = await self._wait_until(inp, expire_at)
        if closed:
            return self._done(closed)
        await workflow.execute_activity("checkout_step", self._step(inp, "expire"), **ACT)
        return self._done("expired")
