"""BillingSweepWorkflow (P8.6, ADR-0022): Nirantar's billing clock.

Every few hours, for every active business: create the debits that fall due within the horizon for subscriptions
Nirantar collects (pay-by-link, mandate). Each new debit emits `subscription.debit_scheduled`, and the event bridge
starts its DebitCycleWorkflow — prediction, notice, collection, verification and recovery all follow from there.
Idempotent: a debit exists at most once per subscription and due date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from nirantar.workflows.platform import SweepInput

ACT: dict[str, Any] = {"start_to_close_timeout": timedelta(minutes=5),
                       "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=5)}


@dataclass
class BillingSweepInput:
    tenants: list[str] = field(default_factory=list)      # empty = every active business with a provider


@dataclass
class BillingStep:
    tenant_id: str
    now_iso: str


@workflow.defn
class BillingSweepWorkflow:
    @workflow.run
    async def run(self, inp: BillingSweepInput) -> dict[str, Any]:
        tenants = inp.tenants or await workflow.execute_activity("sweep_list_tenants", SweepInput(), **ACT)
        created = 0
        for t in tenants:
            r: dict[str, Any] = await workflow.execute_activity(
                "billing_ensure_debits", BillingStep(t, workflow.now().isoformat()), **ACT)
            created += int(r["created"])
        return {"tenants": len(tenants), "debits_created": created}


@dataclass
class BillingDeps:
    engine: Engine


class BillingActivities:
    def __init__(self, deps: BillingDeps) -> None:
        self.d = deps

    @activity.defn(name="billing_ensure_debits")
    def billing_ensure_debits(self, step: BillingStep) -> dict[str, Any]:
        from nirantar.billing.plans import ensure_debits
        from nirantar.db.session import tenant_tx

        now = datetime.fromisoformat(step.now_iso)
        with tenant_tx(step.tenant_id, self.d.engine) as c:
            ids = ensure_debits(c, step.tenant_id, now.date(), now)
        return {"created": len(ids), "debit_ids": ids[:50]}

    def all(self) -> list[Any]:
        return [self.billing_ensure_debits]
