"""Platform workflows (P5, ADR-0015): tenant onboarding and the reconciliation sweep.

OnboardingWorkflow   connect → verify the provider credentials → import history (data pipeline) → features, retention
                     fit → readiness report → status recorded on the tenant. Each step is an activity with retries;
                     a failure stops with a reason the onboarding UI shows.
ReconciliationSweepWorkflow  (Temporal Schedule, every 30 min) for every tenant with a connected provider: pull
                     provider truth for debits stuck without a webhook, and re-process raw webhook events whose
                     processing failed transiently. Missed or late webhooks therefore never leave money state wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

PLATFORM_QUEUE = "nirantar-platform"
SHORT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=2),
         "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)}
LONG: dict[str, Any] = {"start_to_close_timeout": timedelta(hours=1),
        "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=30), maximum_attempts=3)}


@dataclass
class OnboardingInput:
    tenant_id: str
    backfill_days: int = 365


@dataclass
class SweepInput:
    stale_minutes: int = 60
    tenants: list[str] = field(default_factory=list)      # empty = every tenant with a connected provider


def onboarding_workflow_id(tenant_id: str) -> str:
    return f"onboarding:{tenant_id}"


@workflow.defn
class OnboardingWorkflow:
    def __init__(self) -> None:
        self.stage = "init"
        self.steps: list[dict[str, Any]] = []

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "steps": self.steps}

    async def _step(self, name: str, inp: OnboardingInput, opts: dict[str, Any]) -> dict[str, Any]:
        self.stage = name
        out: dict[str, Any] = await workflow.execute_activity(name, inp, **opts)
        self.steps.append({"step": name, "at": workflow.now().isoformat(), "result": out})
        return out

    @workflow.run
    async def run(self, inp: OnboardingInput) -> dict[str, Any]:
        await self._step("onboarding_mark", inp, SHORT)
        check = await self._step("onboarding_verify_provider", inp, SHORT)
        if not check["ok"]:
            self.stage = "failed"
            await workflow.execute_activity("onboarding_finish", {"tenant_id": inp.tenant_id, "ok": False,
                                                                  "reason": check["reason"]}, **SHORT)
            return {"ok": False, "reason": check["reason"], "steps": self.steps}
        data = await self._step("onboarding_import_history", inp, LONG)
        models = await self._step("onboarding_features_and_fits", inp, LONG)
        self.stage = "done"
        await workflow.execute_activity("onboarding_finish", {"tenant_id": inp.tenant_id, "ok": True,
                                                              "summary": {**data, **models}}, **SHORT)
        return {"ok": True, "steps": self.steps}


@workflow.defn
class ReconciliationSweepWorkflow:
    @workflow.run
    async def run(self, inp: SweepInput) -> dict[str, Any]:
        tenants = inp.tenants or await workflow.execute_activity("sweep_list_tenants", inp, **SHORT)
        totals = {"tenants": 0, "debits_changed": 0, "events_reprocessed": 0, "errors": 0}
        for t in tenants:
            out: dict[str, Any] = await workflow.execute_activity(
                "sweep_reconcile_tenant", {"tenant_id": t, "stale_minutes": inp.stale_minutes}, **SHORT)
            totals["tenants"] += 1
            totals["debits_changed"] += out.get("debits_changed", 0)
            totals["events_reprocessed"] += out.get("events_reprocessed", 0)
            totals["errors"] += 1 if out.get("error") else 0
        return totals
