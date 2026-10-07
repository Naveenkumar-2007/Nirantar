"""The Temporal worker: every Nirantar workflow and activity on one task queue, plus the reconciliation schedule."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.worker import Worker

from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.activities import DebitActivities, Deps
from nirantar.workflows.billing import BillingActivities, BillingDeps, BillingSweepInput, BillingSweepWorkflow
from nirantar.workflows.debit_cycle import DebitCycleWorkflow
from nirantar.workflows.dispute import DisputeWorkflow
from nirantar.workflows.dispute_activities import DisputeActivities, DisputeDeps
from nirantar.workflows.health import HealthActivities, HealthDeps, HealthInput, PaymentHealthWorkflow
from nirantar.workflows.mandate import MandateRepairWorkflow, MandateSweepInput, MandateSweepWorkflow
from nirantar.workflows.mandate_activities import MandateActivities, MandateDeps
from nirantar.workflows.platform import OnboardingWorkflow, ReconciliationSweepWorkflow, SweepInput
from nirantar.workflows.platform_activities import PlatformActivities, PlatformDeps
from nirantar.workflows.receivables import InvoiceChaseWorkflow
from nirantar.workflows.receivables_activities import ReceivablesActivities, ReceivablesDeps
from nirantar.workflows.recovery import RecoveryBatchWorkflow
from nirantar.workflows.recovery_activities import RecoveryActivities, RecoveryDeps
from nirantar.workflows.revival import RevivalWorkflow
from nirantar.workflows.revival_activities import RevivalActivities, RevivalDeps

WORKFLOWS = [DebitCycleWorkflow, RevivalWorkflow, DisputeWorkflow, OnboardingWorkflow, ReconciliationSweepWorkflow,
             MandateSweepWorkflow, MandateRepairWorkflow, RecoveryBatchWorkflow, BillingSweepWorkflow,
             PaymentHealthWorkflow, InvoiceChaseWorkflow]
SWEEP_SCHEDULE_ID = "nirantar-reconciliation-sweep"
MANDATE_SCHEDULE_ID = "nirantar-mandate-health"
BILLING_SCHEDULE_ID = "nirantar-billing-sweep"
HEALTH_SCHEDULE_ID = "nirantar-payment-health"


@dataclass
class WorkerDeps:
    engine: Engine
    provider: Any                 # ProviderResolver (each tenant's own account); tests may inject a fixed provider
    comms: Any
    llm: Any = None
    features: Any = None          # OnlineStore
    router: Any = None            # ModelRouter
    lake: Any = None
    environment: str = "local"
    owner: Any = None             # owner engine: platform-level aggregates (payment health)


def activities(d: WorkerDeps) -> list[Any]:
    return [*DebitActivities(Deps(d.engine, d.provider, d.comms, d.llm, d.features, d.router, d.environment)).all(),
            *RevivalActivities(RevivalDeps(d.engine, d.provider, d.comms, d.environment)).all(),
            *DisputeActivities(DisputeDeps(d.engine, d.provider, d.llm, d.environment)).all(),
            *PlatformActivities(PlatformDeps(d.engine, d.provider, d.lake)).all(),
            *MandateActivities(MandateDeps(d.engine, d.provider, d.comms, d.environment)).all(),
            *RecoveryActivities(RecoveryDeps(d.engine, d.provider, d.comms, d.environment)).all(),
            *BillingActivities(BillingDeps(d.engine)).all(),
            *HealthActivities(HealthDeps(d.engine, d.owner or d.engine, d.provider, d.comms, d.environment)).all(),
            *ReceivablesActivities(ReceivablesDeps(d.engine, d.provider, d.comms, d.environment)).all()]


def build_worker(client: Client, d: WorkerDeps, task_queue: str = TASK_QUEUE, threads: int = 16) -> Worker:
    # build id = the deployed code version (e.g. git sha); recorded on every workflow task for diagnosis. Safety of
    # code changes for RUNNING workflows comes from replay tests (tests/replay) + workflow.patched, not routing.
    return Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=activities(d),
                  activity_executor=ThreadPoolExecutor(threads), max_concurrent_activities=threads,
                  build_id=os.environ.get("NIRANTAR_BUILD_ID", "dev"))


async def _ensure(client: Client, schedule_id: str, action: ScheduleActionStartWorkflow, every: timedelta) -> str:
    """Create a schedule once; an existing one is left as operators configured it."""
    try:
        await client.create_schedule(schedule_id, Schedule(
            action=action, spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP)))
        return "created"
    except ScheduleAlreadyRunningError:
        return "exists"


async def ensure_sweep_schedule(client: Client, task_queue: str = TASK_QUEUE,
                                every: timedelta | None = None) -> str:
    every = every or timedelta(minutes=int(os.environ.get("NIRANTAR_RECON_INTERVAL_MIN", "30")))
    return await _ensure(client, SWEEP_SCHEDULE_ID, ScheduleActionStartWorkflow(
        ReconciliationSweepWorkflow.run, SweepInput(), id="reconciliation-sweep", task_queue=task_queue), every)


async def ensure_mandate_schedule(client: Client, task_queue: str = TASK_QUEUE,
                                  every: timedelta | None = None) -> str:
    every = every or timedelta(hours=int(os.environ.get("NIRANTAR_MANDATE_SWEEP_HOURS", "24")))
    return await _ensure(client, MANDATE_SCHEDULE_ID, ScheduleActionStartWorkflow(
        MandateSweepWorkflow.run, MandateSweepInput(), id="mandate-health-sweep", task_queue=task_queue), every)


async def ensure_billing_schedule(client: Client, task_queue: str = TASK_QUEUE,
                                  every: timedelta | None = None) -> str:
    """Nirantar's billing clock: create due debits for pay-by-link and mandate subscriptions (ADR-0022)."""
    every = every or timedelta(hours=int(os.environ.get("NIRANTAR_BILLING_SWEEP_HOURS", "6")))
    return await _ensure(client, BILLING_SCHEDULE_ID, ScheduleActionStartWorkflow(
        BillingSweepWorkflow.run, BillingSweepInput(), id="billing-sweep", task_queue=task_queue), every)


async def ensure_health_schedule(client: Client, task_queue: str = TASK_QUEUE,
                                 every: timedelta | None = None) -> str:
    """Payment health: detect issuer incidents and recover what they broke (ADR-0024)."""
    every = every or timedelta(minutes=int(os.environ.get("NIRANTAR_HEALTH_SCAN_MIN", "15")))
    return await _ensure(client, HEALTH_SCHEDULE_ID, ScheduleActionStartWorkflow(
        PaymentHealthWorkflow.run, HealthInput(), id="payment-health", task_queue=task_queue), every)
