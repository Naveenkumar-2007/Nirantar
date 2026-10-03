"""Soak: the always-on services under continuous traffic, then invariants (P5, ADR-0015).

Real Postgres, Redpanda (Kafka), Temporal server (localhost:7233) and object store. The three services run
in-process under the same supervisor as production (`nirantar.services.runner`), sharing one MockProvider through the
per-tenant resolver. Traffic per tick: new subscriptions with future debits, provider debits on past debits (success
and failure) whose webhooks are delivered TWICE, and chargebacks that the network later decides.

Invariants checked at the end:
  * outbox fully drained (every soak event published to Kafka)
  * every raw webhook processed; duplicate deliveries stored once; one payment row per provider payment
  * every future debit has exactly one DebitCycleWorkflow (deterministic ids; replays rejected)
  * every dispute contested exactly once and closed with the network's outcome
  * nothing dead-lettered; /health stayed green

    NIRANTAR_SOAK=1 NIRANTAR_SOAK_SECONDS=120 uv run pytest tests/soak -q
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import schedule_debit
from nirantar.comms.sink import MockCommsSink
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.providers.resolver import ProviderResolver
from nirantar.services.bridge import BridgeRunner, EventBridge, MemorySeen
from nirantar.services.runner import Component, Services, relay_loop, supervise, worker_loop
from nirantar.services.worker import WorkerDeps, build_worker
from nirantar.workflows.bridge import workflow_id
from nirantar.workflows.dispute import dispute_workflow_id

pytestmark = [pytest.mark.integration, pytest.mark.soak]
SECONDS = float(os.environ.get("NIRANTAR_SOAK_SECONDS", "60"))
TENANTS = int(os.environ.get("NIRANTAR_SOAK_TENANTS", "3"))


@pytest.fixture(autouse=True)
def _opt_in() -> None:
    if os.environ.get("NIRANTAR_SOAK") != "1":
        pytest.skip("soak runs only with NIRANTAR_SOAK=1")


def _setup_tenant(engine: Engine, mock: MockProvider) -> str:
    from nirantar.billing.service import connect_provider, create_tenant

    t = new_id("ten")
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, f"Soak merchant {t[-4:]}", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_soak")
    return t


def _new_subscriber(engine: Engine, mock: MockProvider, t: str) -> tuple[str, str, str]:
    from nirantar.billing.service import NewCustomer, create_customer, create_subscription

    with tenant_tx(t, engine) as c:
        cust = create_customer(c, t, NewCustomer(new_id("ext"), "Soak", None, None, consents={"sms": True}))
        mid = new_id("mdt")
        c.execute(text("INSERT INTO billing.mandates (tenant_id, mandate_id, customer_id, provider, rail, "
                       "max_amount_minor, status) VALUES (:t, :m, :c, 'mock', 'upi_autopay', 500000, 'active')"),
                  {"t": t, "m": mid, "c": cust})
        amount = Money.of(str(random.choice([199, 499, 999, 1499])))
        psub = mock.add_subscription(cust, amount)
        sub = create_subscription(c, t, cust, "mock", psub, amount)
        c.execute(text("UPDATE billing.subscriptions SET mandate_id=:m WHERE subscription_id=:s"), {"m": mid, "s": sub})
    return cust, sub, psub


class Traffic:
    def __init__(self, engine: Engine, mock: MockProvider, tenants: list[str]) -> None:
        self.e, self.mock, self.tenants = engine, mock, tenants
        self.future_debits: list[tuple[str, str]] = []
        self.webhooks = 0
        self.disputes: dict[str, tuple[str, str]] = {}           # provider dispute id → (tenant, outcome or "")
        self.captured: list[tuple[str, str]] = []                # (tenant, provider payment id)

    def deliver(self, t: str, provider_type: str, key: str, entity: dict[str, Any]) -> None:
        h, b = self.mock.webhook_for(provider_type, key, entity)
        for _ in range(2):                                          # providers retry: every webhook arrives twice
            assert ingest_webhook(self.e, self.mock, t, h, b).status_code == 200
        self.webhooks += 1

    def tick(self) -> None:
        now = datetime.now(UTC)
        t = random.choice(self.tenants)
        _, sub, psub = _new_subscriber(self.e, self.mock, t)
        r = random.random()
        with tenant_tx(t, self.e) as c:
            if r < 0.5:                                             # a debit next week → delayed DebitCycle
                debit = schedule_debit(c, t, sub, (now + timedelta(days=random.randint(5, 9))).date(), now)
                self.future_debits.append((t, debit))
                return
            schedule_debit(c, t, sub, (now - timedelta(days=15)).date(), now - timedelta(days=20))
        ok = random.random() < 0.8                                   # a past debit the provider executed
        pay = self.mock.charge(psub, succeed=ok, error_code=None if ok else "BAD_REQUEST_ERROR",
                               at=now - timedelta(days=15))
        self.deliver(t, "payment.captured" if ok else "payment.failed", "payment", MockProvider.payment_entity(pay))
        if ok:
            self.captured.append((t, pay.provider_payment_id))

    def chargeback(self) -> None:
        if not self.captured:
            return
        t, pid = self.captured.pop(random.randrange(len(self.captured)))
        d = self.mock.open_dispute(pid, "unauthorised_recurring", respond_days=7)
        self.disputes[d.provider_dispute_id] = (t, "")
        self.deliver(t, "payment.dispute.created", "dispute", MockProvider.dispute_entity(d))

    def decide(self) -> None:
        for pdid, (t, outcome) in list(self.disputes.items()):
            if outcome or self.mock.disputes[pdid].status != "submitted":
                continue
            result = random.choice(["won", "lost"])
            self.disputes[pdid] = (t, result)
            d = self.mock.resolve_dispute(pdid, result)
            self.deliver(t, f"payment.dispute.{result}", "dispute", MockProvider.dispute_entity(d))


@pytest.mark.asyncio
async def test_services_soak(app_engine: Engine) -> None:
    from temporalio.client import Client

    try:
        client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    except Exception:
        pytest.skip("Temporal server not reachable")
    from nirantar.events.relay import KafkaProducer, relay_engine

    random.seed(26)
    mock = MockProvider(webhook_secret="whsec_soak")
    tenants = [_setup_tenant(app_engine, mock) for _ in range(TENANTS)]
    queue, stop, svc = f"soak-{new_id('que')}", asyncio.Event(), Services()
    resolver = ProviderResolver(app_engine, injected={"mock": mock})
    bridge = EventBridge(app_engine, client, resolver, task_queue=queue)
    runner = BridgeRunner(bridge, MemorySeen(), only_tenants=frozenset(tenants))
    deps = WorkerDeps(app_engine, resolver, MockCommsSink())
    relay_eng, producer = relay_engine(), KafkaProducer()
    svc.stats = {"bridge": bridge.stats, "relay": {}}

    async def run_worker(c: Component) -> None:
        await worker_loop(c, stop, build_worker(client, deps, task_queue=queue))

    async def run_relay(c: Component) -> None:
        await relay_loop(c, stop, relay_eng, producer, svc.stats["relay"])

    async def run_bridge(c: Component) -> None:
        await runner.run_kafka(stop, group_id=f"soak-{new_id('grp')}", heartbeat=c.beat)

    tasks = [asyncio.create_task(supervise(svc, n, f, stop))
             for n, f in (("worker", run_worker), ("relay", run_relay), ("event_bridge", run_bridge))]
    traffic = Traffic(app_engine, mock, tenants)
    health_samples: list[bool] = []
    started = time.monotonic()
    try:
        await asyncio.sleep(3)                                       # consumer group join
        while time.monotonic() - started < SECONDS:
            await asyncio.to_thread(traffic.tick)
            if random.random() < 0.15:
                await asyncio.to_thread(traffic.chargeback)
            await asyncio.to_thread(traffic.decide)
            health_samples.append(svc.health()[0])
            await asyncio.sleep(0.25)

        # settle: wait until every dispute is decided and closed, and the pipeline is idle
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            await asyncio.to_thread(traffic.decide)
            closed = 0
            for t, outcome in traffic.disputes.values():
                if not outcome:
                    continue
                with tenant_tx(t, app_engine) as c:
                    closed += c.execute(text("SELECT count(*) FROM ops.cases WHERE kind='dispute' AND "
                                             "status='closed'")).scalar_one()
            decided = sum(1 for _, o in traffic.disputes.values() if o)
            if decided == len(traffic.disputes) and closed >= decided and await _idle(app_engine, tenants):
                break
            await asyncio.sleep(1)
        report = await _invariants(app_engine, client, traffic, tenants, queue)
        report.update({"seconds": SECONDS, "health_green_share": sum(health_samples) / max(1, len(health_samples)),
                       "bridge": dict(bridge.stats), "relay_published": svc.stats["relay"].get("published", 0),
                       "restarts": {n: c.restarts for n, c in svc.components.items()}})
        print("\nSOAK REPORT", json.dumps(report, indent=1, default=str))
        assert report["health_green_share"] == 1.0
        assert all(v == 0 for v in report["restarts"].values())
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for t, d in traffic.future_debits:                           # delayed cycles: clean up the test server
            try:
                await client.get_workflow_handle(workflow_id(t, d)).terminate("soak done")
            except Exception:  # noqa: S110 - best-effort cleanup
                pass
        relay_eng.dispose()


async def _idle(engine: Engine, tenants: list[str]) -> bool:
    for t in tenants:
        with tenant_tx(t, engine) as c:
            pending = c.execute(text("SELECT (SELECT count(*) FROM events.outbox WHERE published_at IS NULL) + "
                                     "(SELECT count(*) FROM ingest.provider_events WHERE status='received')")
                                ).scalar_one()
        if pending:
            return False
    return True


async def _invariants(engine: Engine, client: Any, traffic: Traffic, tenants: list[str],
                      queue: str) -> dict[str, Any]:
    out: dict[str, Any] = {"tenants": len(tenants), "webhooks_sent": traffic.webhooks * 2,
                           "future_debits": len(traffic.future_debits), "disputes": len(traffic.disputes)}
    unpublished = raw_total = raw_unprocessed = dup_payments = dead = 0
    for t in tenants:
        with tenant_tx(t, engine) as c:
            unpublished += c.execute(text("SELECT count(*) FROM events.outbox WHERE published_at IS NULL")).scalar_one()
            raw_total += c.execute(text("SELECT count(*) FROM ingest.provider_events")).scalar_one()
            raw_unprocessed += c.execute(text("SELECT count(*) FROM ingest.provider_events WHERE status<>'processed'")
                                         ).scalar_one()
            dup_payments += c.execute(text("SELECT count(*) FROM (SELECT provider_payment_id FROM billing.payments "
                                           "GROUP BY 1 HAVING count(*) > 1) x")).scalar_one()
            dead += c.execute(text("SELECT count(*) FROM events.consumer_dead_letters")).scalar_one()
    out.update({"outbox_unpublished": unpublished, "raw_events_stored": raw_total,
                "raw_events_unprocessed": raw_unprocessed, "duplicate_payment_rows": dup_payments,
                "dead_letters": dead})
    assert unpublished == 0, "outbox not drained"
    assert raw_total == traffic.webhooks, "duplicate webhook deliveries must be stored once"
    assert raw_unprocessed == 0 and dup_payments == 0 and dead == 0

    missing = 0
    for t, d in traffic.future_debits:
        desc = await client.get_workflow_handle(workflow_id(t, d)).describe()
        missing += desc.workflow_type != "DebitCycleWorkflow" or desc.task_queue != queue
    out["debit_workflows_missing"] = missing
    assert missing == 0

    outcomes: dict[str, int] = {}
    for pdid, (t, outcome) in traffic.disputes.items():
        with tenant_tx(t, engine) as c:
            did, status = c.execute(text("SELECT dispute_id, status FROM billing.disputes WHERE "
                                         "provider_dispute_id=:p"), {"p": pdid}).one()
        contests = sum(1 for x in traffic.mock.contests if x["dispute"] == pdid)
        if contests != 1:
            wf = await client.get_workflow_handle(dispute_workflow_id(t, did)).query("status")
            raise AssertionError(f"dispute {pdid} contested {contests}x; db={status}; workflow={wf}")
        res = await client.get_workflow_handle(dispute_workflow_id(t, did)).result()
        assert status == outcome and res["outcome"] == outcome
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    out["dispute_outcomes"] = outcomes
    return out
