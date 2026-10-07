"""RecoveryBatchWorkflow (P8.5, ADR-0021): run an operator-launched recovery batch to a verified, measured close.

execute every treatment item (through the MCP gateway, idempotent per item) → items refused only because the
customer's contact window is closed are retried when it opens (at most MAX_ROUNDS) → wait for the measurement window
(or a stop) → measure verified outcomes against the holdout and close.

Deterministic: no I/O here, time from workflow.now(); every side effect is an activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=2),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=6)}
MAX_ROUNDS = 3


@dataclass
class BatchInput:
    tenant_id: str
    batch_id: str
    ends_at_iso: str


@dataclass
class BatchStep:
    tenant_id: str
    batch_id: str
    now_iso: str
    item_id: str = ""


def batch_workflow_id(tenant_id: str, batch_id: str) -> str:
    return f"recovery-batch:{tenant_id}:{batch_id}"


@workflow.defn
class RecoveryBatchWorkflow:
    def __init__(self) -> None:
        self._stop = False
        self.done: dict[str, str] = {}

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stopped": self._stop, "done": len(self.done), "states": dict(self.done)}

    def _step(self, inp: BatchInput, item_id: str = "") -> BatchStep:
        return BatchStep(inp.tenant_id, inp.batch_id, workflow.now().isoformat(), item_id)

    @workflow.run
    async def run(self, inp: BatchInput) -> dict[str, Any]:
        ends = datetime.fromisoformat(inp.ends_at_iso)
        pending: list[str] = await workflow.execute_activity("rb_items", self._step(inp), **ACT)
        for _ in range(MAX_ROUNDS):
            later: list[tuple[str, datetime]] = []
            for item_id in pending:
                if self._stop:
                    break
                r: dict[str, Any] = await workflow.execute_activity("rb_execute", self._step(inp, item_id), **ACT)
                self.done[item_id] = r["state"]
                if r["state"] == "denied" and r.get("retry_after"):
                    later.append((item_id, datetime.fromisoformat(r["retry_after"])))
            later = [(i, t) for i, t in later if t < ends]
            if self._stop or not later:
                break
            wake = min(t for _, t in later)
            try:
                await workflow.wait_condition(lambda: self._stop, timeout=max(timedelta(0), wake - workflow.now()))
            except TimeoutError:
                pass
            pending = [i for i, _ in later]
        if not self._stop and workflow.now() < ends:
            try:
                await workflow.wait_condition(lambda: self._stop, timeout=ends - workflow.now())
            except TimeoutError:
                pass
        report: dict[str, Any] = await workflow.execute_activity("rb_close", self._step(inp), **ACT)
        return report
