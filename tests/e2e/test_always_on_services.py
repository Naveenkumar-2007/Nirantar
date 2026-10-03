"""P5 always-on services against real Postgres + object store + a Temporal test server (ADR-0015).

The event bridge is fed the tenant's committed outbox envelopes in order (exactly what the relay publishes to
Kafka; the Kafka transport itself is covered by test_event_backbone and the soak run). Provider = MockProvider,
injected through the per-tenant ProviderResolver exactly as the services inject a real account.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.providers.resolver import ProviderResolver
from nirantar.services.bridge import BridgeRunner, EventBridge, MemorySeen, debit_timing
from nirantar.services.worker import WORKFLOWS, WorkerDeps, activities
from nirantar.settings.schema import Operations, platform_defaults
from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.bridge import workflow_id
from nirantar.workflows.dispute import DisputeWorkflow, dispute_workflow_id
from nirantar.workflows.platform import OnboardingInput, OnboardingWorkflow, ReconciliationSweepWorkflow, SweepInput
from tests.replay.capture import capture

pytestmark = pytest.mark.integration


class Pump:
    """Feeds a tenant's outbox (in commit order) through the bridge, once per event — the relay's job."""

    def __init__(self, engine: Engine, tenant: str, runner: BridgeRunner) -> None:
        self.engine, self.tenant, self.runner, self.done = engine, tenant, runner, set[str]()
        self.outcomes: list[tuple[str, str]] = []

    async def drain(self) -> list[tuple[str, str]]:
        new: list[tuple[str, str]] = []
        for _ in range(10):                      # handling an event can emit more (webhook → payment.captured)
            with tenant_tx(self.tenant, self.engine) as c:
                rows = c.execute(text("SELECT event_id, event_type, envelope FROM events.outbox ORDER BY created_at, "
                                      "event_id")).all()
            todo = [r for r in rows if r.event_id not in self.done]
            if not todo:
                break
            for r in todo:
                self.done.add(r.event_id)
                env = r.envelope if isinstance(r.envelope, dict) else json.loads(r.envelope)
                out = await self.runner.process(json.dumps(env).encode())
                new.append((r.event_type, out))
        self.outcomes += new
        return new

    async def replay_all(self) -> list[str]:
        with tenant_tx(self.tenant, self.engine) as c:
            envs = [r[0] for r in c.execute(text("SELECT envelope FROM events.outbox ORDER BY created_at")).all()]
        return [await self.runner.process(json.dumps(e if isinstance(e, dict) else json.loads(e)).encode())
                for e in envs]


def _merchant(engine: Engine, mock: MockProvider, *, debit_on: date, now: datetime) -> dict[str, Any]:
    t = new_id("ten")
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, "Chai Club", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_p5")
        cust = create_customer(c, t, NewCustomer(new_id("ext"), "Priya", None, None, consents={"sms": True}))
        mid = new_id("mdt")
        c.execute(text("INSERT INTO billing.mandates (tenant_id, mandate_id, customer_id, provider, rail, "
                       "max_amount_minor, status) VALUES (:t, :m, :c, 'mock', 'upi_autopay', 200000, 'active')"),
                  {"t": t, "m": mid, "c": cust})
        psub = mock.add_subscription(cust, Money.of("999"))
        sub = create_subscription(c, t, cust, "mock", psub, Money.of("999"))
        c.execute(text("UPDATE billing.subscriptions SET mandate_id=:m WHERE subscription_id=:s"), {"m": mid, "s": sub})
        debit = schedule_debit(c, t, sub, debit_on, now)
    return {"tenant": t, "customer": cust, "provider_sub": psub, "subscription": sub, "debit": debit}


async def _wait(predicate: Any, timeout_s: float = 60) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not await predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.2)


def test_debit_timing_from_tenant_settings() -> None:
    ops = Operations.model_validate(platform_defaults()["namespaces"]["operations"])
    debit_at, start_at = debit_timing(ops, date(2026, 10, 20))
    assert debit_at == datetime(2026, 10, 20, 4, 30, tzinfo=UTC)          # 10:00 IST
    assert start_at == debit_at - timedelta(days=3)                       # workflow starts at T-3 (notice day)


def test_dispute_that_overtakes_its_payment_is_still_linked(app_engine: Engine) -> None:
    mock, now = MockProvider(webhook_secret="whsec_p5"), datetime.now(UTC)
    m = _merchant(app_engine, mock, debit_on=(now - timedelta(days=3)).date(), now=now - timedelta(days=6))
    pay = mock.charge(m["provider_sub"], succeed=True, at=now - timedelta(days=3))   # its webhook never came
    disp = mock.open_dispute(pay.provider_payment_id, at=now)
    h, b = mock.webhook_for("payment.dispute.created", "dispute", MockProvider.dispute_entity(disp))
    raw = ingest_webhook(app_engine, mock, m["tenant"], h, b).raw_event_id
    assert process_raw_event(app_engine, mock, m["tenant"], str(raw)).event_type == "dispute.opened"
    with tenant_tx(m["tenant"], app_engine) as c:
        assert c.execute(text("SELECT debit_id FROM billing.disputes")).scalar_one() == m["debit"]
        assert c.execute(text("SELECT status FROM billing.debits")).scalar_one() == "succeeded"
        assert c.execute(text("SELECT count(*) FROM billing.payments")).scalar_one() == 1


@pytest.mark.asyncio
async def test_dispute_lifecycle_through_bridge_and_workflow(app_engine: Engine) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    mock = MockProvider(webhook_secret="whsec_p5")
    now = datetime.now(UTC)
    m = _merchant(app_engine, mock, debit_on=(now - timedelta(days=20)).date(), now=now - timedelta(days=25))
    t = m["tenant"]
    with tenant_tx(t, app_engine) as c:                     # the notice went out 2 days before the debit
        c.execute(text("UPDATE billing.debits SET predebit_notified_at=:n WHERE debit_id=:d"),
                  {"n": now - timedelta(days=22), "d": m["debit"]})
    resolver = ProviderResolver(app_engine, injected={"mock": mock})
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        bridge = EventBridge(app_engine, env.client, resolver)
        pump = Pump(app_engine, t, BridgeRunner(bridge, MemorySeen()))
        deps = WorkerDeps(app_engine, resolver, comms=None)
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=activities(deps),
                          activity_executor=ThreadPoolExecutor(8)):
            # the debit is long past: the bridge must not start a customer-facing cycle for it
            assert ("subscription.debit_scheduled", "skipped_stale") in await pump.drain()

            # the provider collects the payment; webhook → bridge processes it with the tenant's account
            pay = mock.charge(m["provider_sub"], succeed=True, at=now - timedelta(days=20))
            h, b = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(pay))
            assert ingest_webhook(app_engine, mock, t, h, b).status_code == 200
            out = await pump.drain()
            assert ("provider.webhook_received", "processed") in out
            assert ("payment.captured", "no_workflow") in out

            # chargeback → dispute.opened → DisputeWorkflow contests with the evidence pack
            disp = mock.open_dispute(pay.provider_payment_id, "unauthorised_recurring", at=now, respond_days=7)
            h, b = mock.webhook_for("payment.dispute.created", "dispute", MockProvider.dispute_entity(disp))
            ingest_webhook(app_engine, mock, t, h, b)
            out = await pump.drain()
            assert ("dispute.opened", "started") in out
            with tenant_tx(t, app_engine) as c:
                dispute_id = c.execute(text("SELECT dispute_id FROM billing.disputes")).scalar_one()
            handle = env.client.get_workflow_handle(dispute_workflow_id(t, dispute_id))

            async def stage_is_waiting() -> bool:
                st = await handle.query(DisputeWorkflow.status)
                return bool(st["stage"] == "waiting_outcome")

            await _wait(stage_is_waiting)
            assert len(mock.contests) == 1 and mock.contests[0]["submit"] is True
            doc = mock.contests[0]["documents"]["proof_of_service"][0]
            assert mock.documents[doc][0] == "application/pdf" and mock.documents[doc][1] > 1000
            with tenant_tx(t, app_engine) as c:
                assert c.execute(text("SELECT status FROM billing.disputes")).scalar_one() == "submitted"
                assert c.execute(text("SELECT count(*) FROM ai.evidence WHERE case_id LIKE 'cas_dp%'")).scalar_one() == 4

            # the network decides: won → dispute.updated → signal → close with a provider-sourced label
            mock.resolve_dispute(disp.provider_dispute_id, "won")
            h, b = mock.webhook_for("payment.dispute.won", "dispute",
                                    MockProvider.dispute_entity(mock.disputes[disp.provider_dispute_id]))
            ingest_webhook(app_engine, mock, t, h, b)
            assert ("dispute.updated", "signalled") in await pump.drain()
            result = await handle.result()
            await capture(handle, "DisputeWorkflow")
            assert result["outcome"] == "won"
            with tenant_tx(t, app_engine) as c:
                assert c.execute(text("SELECT status FROM billing.disputes")).scalar_one() == "won"
                assert c.execute(text("SELECT status FROM ops.cases WHERE kind='dispute'")).scalar_one() == "closed"

            # at-least-once delivery: replaying every event changes nothing
            fresh = BridgeRunner(bridge, MemorySeen())
            pump.runner = fresh
            replayed = await pump.replay_all()
            assert "started" not in replayed and len(mock.contests) == 1
    finally:
        await env.shutdown()


@pytest.mark.asyncio
async def test_debit_cycle_started_once_and_bad_events_dead_lettered(app_engine: Engine) -> None:
    from temporalio.testing import WorkflowEnvironment

    mock = MockProvider(webhook_secret="whsec_p5")
    now = datetime.now(UTC)
    m = _merchant(app_engine, mock, debit_on=(now + timedelta(days=6)).date(), now=now)
    t = m["tenant"]
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        bridge = EventBridge(app_engine, env.client, ProviderResolver(app_engine, injected={"mock": mock}))
        runner = BridgeRunner(bridge, MemorySeen(), max_backoff_s=0.2)
        pump = Pump(app_engine, t, runner)
        assert ("subscription.debit_scheduled", "started") in await pump.drain()
        desc = await env.client.get_workflow_handle(workflow_id(t, m["debit"])).describe()
        assert desc.workflow_type == "DebitCycleWorkflow"
        pump.runner = BridgeRunner(bridge, MemorySeen())
        assert "already_started" in await pump.replay_all()               # exactly one workflow per debit
        with tenant_tx(t, app_engine) as c:                                 # tenant got a recovery experiment
            assert c.execute(text("SELECT count(*) FROM experiments.experiments WHERE name='recovery'")).scalar_one() == 1
        await env.client.get_workflow_handle(workflow_id(t, m["debit"])).terminate("test done")

        # out-of-order delivery: payment.failed overtakes subscription.debit_scheduled (different topics)
        late = _merchant(app_engine, mock, debit_on=(now - timedelta(days=1)).date(), now=now - timedelta(days=4))
        lp = Pump(app_engine, late["tenant"], BridgeRunner(bridge, MemorySeen()))
        with tenant_tx(late["tenant"], app_engine) as c:
            lp.done |= {r[0] for r in c.execute(text("SELECT event_id FROM events.outbox"))}   # not yet delivered
        failed = mock.charge(late["provider_sub"], succeed=False, error_code="BAD_REQUEST_ERROR")
        h, b = mock.webhook_for("payment.failed", "payment", MockProvider.payment_entity(failed))
        ingest_webhook(app_engine, mock, late["tenant"], h, b)
        assert ("payment.failed", "signalled") in await lp.drain()          # signal-with-start
        lp.done.clear()
        assert "already_started" in await lp.replay_all()                   # the late debit_scheduled: no 2nd run
        lh = env.client.get_workflow_handle(workflow_id(late["tenant"], late["debit"]))
        assert (await lh.describe()).workflow_type == "DebitCycleWorkflow"
        await lh.terminate("test done")

        # a webhook for a provider this tenant never connected: not retryable → dead-letter, partition moves on
        from nirantar.contracts.events import make_event

        bad = make_event(event_type="provider.webhook_received", version=1, tenant_id=t, subject_id="raw_x",
                         payload={"raw_event_id": "raw_x", "provider": "razorpay"}, source="test", occurred_at=now)
        assert await runner.process(bad.model_dump_json().encode()) == "dead_lettered"
        assert await runner.process(b"{not json") == "invalid"
        with tenant_tx(t, app_engine) as c:
            row = c.execute(text("SELECT event_type, attempts, error FROM events.consumer_dead_letters")).one()
        assert row.event_type == "provider.webhook_received" and row.attempts == 3 and "razorpay" in row.error
    finally:
        await env.shutdown()


@pytest.mark.asyncio
async def test_reconciliation_sweep_and_onboarding(app_engine: Engine) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    mock = MockProvider(webhook_secret="whsec_p5")
    now = datetime.now(UTC)
    m = _merchant(app_engine, mock, debit_on=(now - timedelta(days=1)).date(), now=now - timedelta(days=4))
    t = m["tenant"]
    # a webhook that arrived but was never processed (bridge was down), and a debit stuck in 'attempting'
    pay = mock.charge(m["provider_sub"], succeed=True, at=now - timedelta(hours=20))
    h, b = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(pay))
    raw = ingest_webhook(app_engine, mock, t, h, b).raw_event_id
    with tenant_tx(t, app_engine) as c:
        c.execute(text("UPDATE ingest.provider_events SET received_at=:r WHERE raw_event_id=:e"),
                  {"r": now - timedelta(minutes=30), "e": raw})
        c.execute(text("UPDATE billing.debits SET status='attempting', updated_at=:u WHERE debit_id=:d"),
                  {"u": now - timedelta(hours=20), "d": m["debit"]})
    no_provider = new_id("ten")
    with tenant_tx(no_provider, app_engine) as c:
        create_tenant(c, no_provider, "Not connected yet")

    resolver = ProviderResolver(app_engine, injected={"mock": mock})
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                          activities=activities(WorkerDeps(app_engine, resolver, comms=None)),
                          activity_executor=ThreadPoolExecutor(4)):
            sh = await env.client.start_workflow(ReconciliationSweepWorkflow.run,
                                                 SweepInput(stale_minutes=60, tenants=[t, no_provider]),
                                                 id=new_id("sweep"), task_queue=TASK_QUEUE)
            totals = await sh.result()
            await capture(sh, "ReconciliationSweepWorkflow")
            assert totals["tenants"] == 2 and totals["errors"] == 0
            assert totals["debits_changed"] + totals["events_reprocessed"] >= 1
            with tenant_tx(t, app_engine) as c:
                assert c.execute(text("SELECT status FROM ingest.provider_events WHERE raw_event_id=:e"),
                                 {"e": raw}).scalar_one() == "processed"
                assert c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"),
                                 {"d": m["debit"]}).scalar_one() == "succeeded"

            # onboarding stops cleanly, with a reason, when no provider is connected
            oh = await env.client.start_workflow(OnboardingWorkflow.run, OnboardingInput(no_provider),
                                                 id=new_id("onb"), task_queue=TASK_QUEUE)
            res = await oh.result()
            await capture(oh, "OnboardingWorkflow")
            assert res["ok"] is False and "no payment provider" in res["reason"]
            with tenant_tx(no_provider, app_engine) as c:
                s = c.execute(text("SELECT settings FROM core.tenants")).scalar_one()
            s = s if isinstance(s, dict) else json.loads(s)
            assert s["onboarding"]["status"] == "failed"
    finally:
        await env.shutdown()
