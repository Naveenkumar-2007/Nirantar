"""Event bridge: committed domain events (Kafka) → durable work (Temporal) and webhook processing.

    provider.webhook_received   → process the stored raw webhook with the tenant's own provider account
    subscription.debit_scheduled → start DebitCycleWorkflow, delayed until the pre-debit notice day (T-notice)
    payment.captured / failed, reply.received → signal the debit's workflow
    dispute.opened              → start DisputeWorkflow
    dispute.updated             → signal it (or signal-with-start while the dispute is still open)

Delivery is at-least-once, so every handler is idempotent by construction: workflow ids are deterministic and
duplicates are rejected by Temporal; raw webhooks carry a processed status; signals are idempotent in workflows.
The Redis seen-set is only a shortcut. Infrastructure errors (Temporal/DB down) are retried without committing the
offset — an outage pauses the bridge instead of losing events. Other errors are retried a few times, then the event
goes to the tenant's dead-letter table and the Kafka DLQ topic, and the partition moves on.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from nirantar.contracts.events import EventEnvelope
from nirantar.db.session import tenant_tx
from nirantar.events import DLQ_TOPIC, kafka_topic
from nirantar.experiments.service import ensure_recovery_experiment
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.resolver import provider_for
from nirantar.settings import service as settings
from nirantar.settings.schema import Operations
from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.bridge import debit_signal, workflow_id
from nirantar.workflows.checkout import CheckoutInput, CheckoutRecoveryWorkflow, checkout_workflow_id
from nirantar.workflows.debit_cycle import DebitCycleWorkflow
from nirantar.workflows.dispute import DisputeInput, DisputeWorkflow, dispute_workflow_id
from nirantar.workflows.receivables import InvoiceChaseWorkflow, InvoiceInput, invoice_workflow_id
from nirantar.workflows.types import CycleInput

log = structlog.get_logger("event-bridge")
CONSUMER = "event-bridge"
TOPICS = ("provider", "subscription", "payment", "reply", "dispute", "mandate", "invoice", "checkout")
TERMINAL_DISPUTE = ("won", "lost", "accepted")
TRANSIENT_RPC = (RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED, RPCStatusCode.RESOURCE_EXHAUSTED)


class Seen(Protocol):
    def seen(self, event_id: str) -> bool: ...
    def mark(self, event_id: str) -> None: ...


@dataclass
class MemorySeen:
    ids: set[str] = field(default_factory=set)

    def seen(self, event_id: str) -> bool:
        return event_id in self.ids

    def mark(self, event_id: str) -> None:
        self.ids.add(event_id)


class RedisSeen:
    def __init__(self, r: Any = None, ttl_s: int = 7 * 86400) -> None:
        if r is None:
            import redis

            r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:26379/0"))
        self.r, self.ttl = r, ttl_s

    def seen(self, event_id: str) -> bool:
        return bool(self.r.exists(f"bridge:seen:{event_id}"))

    def mark(self, event_id: str) -> None:
        self.r.set(f"bridge:seen:{event_id}", 1, ex=self.ttl)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RPCError):
        return exc.status in TRANSIENT_RPC
    return isinstance(exc, OperationalError | ConnectionError | TimeoutError)


def debit_timing(ops: Operations, scheduled_for: date) -> tuple[datetime, datetime]:
    """(debit_at, workflow start) in UTC from the tenant's debit-cycle settings."""
    cfg = ops.debit_cycle
    hh, mm = (int(x) for x in cfg.debit_time_local.split(":"))
    debit_at = datetime.combine(scheduled_for, time(hh, mm), ZoneInfo(cfg.timezone)).astimezone(UTC)
    return debit_at, debit_at - timedelta(days=cfg.notice_days)


@dataclass
class EventBridge:
    engine: Engine
    client: Client
    provider: Any                                  # ProviderResolver (services) or a fixed provider (tests)
    task_queue: str = TASK_QUEUE
    stats: Counter[str] = field(default_factory=Counter)

    async def handle(self, ev: EventEnvelope) -> str:
        """Do the work for one event. Returns what happened (for stats and tests)."""
        t = ev.event_type
        if t == "provider.webhook_received":
            outcome = await asyncio.to_thread(self._process_webhook, ev)
        elif t == "subscription.debit_scheduled":
            outcome = await self._start_debit_cycle(ev)
        elif t in ("payment.captured", "payment.failed", "reply.received"):
            outcome = await self._signal_debit(ev)
        elif t == "dispute.opened":
            outcome = await self._start_dispute(ev, None)
        elif t == "dispute.updated":
            outcome = await self._dispute_updated(ev)
        elif t == "mandate.updated":
            outcome = await self._mandate_updated(ev)
        elif t == "invoice.issued":
            outcome = await self._start_invoice(ev)
        elif t == "invoice.payment_verified":
            outcome = await self._wake_invoice(ev)
        elif t == "checkout.updated":
            outcome = await self._checkout(ev)
        else:
            outcome = "ignored"
        self.stats[f"{t}:{outcome}"] += 1
        return outcome

    # ------------------------------------------------------------------ handlers
    def _process_webhook(self, ev: EventEnvelope) -> str:
        p = provider_for(self.provider, ev.tenant_id, ev.payload.get("provider"))
        return process_raw_event(self.engine, p, ev.tenant_id, ev.payload["raw_event_id"]).status

    def _plan_cycle(self, tenant_id: str, debit_id: str, customer_id: str | None,
                    scheduled_for: str | None) -> tuple[CycleInput, timedelta] | str:
        with tenant_tx(tenant_id, self.engine) as c:
            row = c.execute(text("SELECT status, customer_id, scheduled_for FROM billing.debits WHERE tenant_id=:t "
                                 "AND debit_id=:d"), {"t": tenant_id, "d": debit_id}).one_or_none()
            if row is None or row.status in ("cancelled", "succeeded"):
                return f"skipped_{row.status if row else 'missing'}"
            ops = settings.model(c, tenant_id, "operations", Operations)
            exp = ensure_recovery_experiment(c, tenant_id)
        debit_at, start_at = debit_timing(ops, date.fromisoformat((scheduled_for or str(row.scheduled_for))[:10]))
        now = datetime.now(UTC)
        cfg = ops.debit_cycle
        if debit_at + timedelta(days=cfg.recovery_window_days) < now:
            return "skipped_stale"                         # long past: reconciliation owns it, no customer contact
        inp = CycleInput(tenant_id, debit_id, customer_id or row.customer_id, exp, debit_at.isoformat(),
                         payment_wait_hours=cfg.payment_wait_hours, recovery_window_days=cfg.recovery_window_days,
                         round_gap_days=cfg.round_gap_days, max_contact_rounds=cfg.max_contact_rounds)
        return inp, max(timedelta(0), start_at - now)

    async def _start_debit_cycle(self, ev: EventEnvelope) -> str:
        planned = await asyncio.to_thread(self._plan_cycle, ev.tenant_id, ev.payload["debit_id"],
                                          ev.payload.get("customer_id"), ev.payload.get("scheduled_for"))
        if isinstance(planned, str):
            return planned
        inp, delay = planned
        try:
            await self.client.start_workflow(
                DebitCycleWorkflow.run, inp, id=workflow_id(inp.tenant_id, inp.debit_id), task_queue=self.task_queue,
                start_delay=delay or None, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:
            return "already_started"
        return "started"

    async def _signal_debit(self, ev: EventEnvelope) -> str:
        """Signal-with-start: Kafka orders events per topic only, so a payment event can overtake its
        subscription.debit_scheduled. Starting the cycle with the signal makes arrival order irrelevant."""
        sig = debit_signal(ev)
        if sig is None:
            return "no_debit"
        planned = await asyncio.to_thread(self._plan_cycle, ev.tenant_id, ev.payload["debit_id"], None, None)
        if not isinstance(planned, str):
            inp, _ = planned
            try:
                await self.client.start_workflow(
                    DebitCycleWorkflow.run, inp, id=workflow_id(inp.tenant_id, inp.debit_id),
                    task_queue=self.task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    start_signal=sig[0], start_signal_args=[sig[1]])
                return "signalled"
            except WorkflowAlreadyStartedError:
                return "cycle_closed"
        try:                                               # nothing to start (paid / stale): signal if one runs
            await self.client.get_workflow_handle(workflow_id(ev.tenant_id, ev.payload["debit_id"])).signal(*sig)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:      # imported history / finished cycle: state is in the DB
                return "no_workflow"
            raise
        return "signalled"

    async def _start_invoice(self, ev: EventEnvelope) -> str:
        """B2B receivables (ADR-0025): one escalation ladder per issued invoice."""
        try:
            await self.client.start_workflow(
                InvoiceChaseWorkflow.run, InvoiceInput(ev.tenant_id, ev.payload["invoice_id"], ev.payload["due_on"]),
                id=invoice_workflow_id(ev.tenant_id, ev.payload["invoice_id"]), task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:
            return "already_started"
        return "started"

    async def _checkout(self, ev: EventEnvelope) -> str:
        """Checkout drop-off recovery (ADR-0028): the first event starts the checkout's workflow; later ones wake it."""
        wid = checkout_workflow_id(ev.tenant_id, ev.payload["session_id"])
        if ev.payload.get("new"):
            try:
                await self.client.start_workflow(
                    CheckoutRecoveryWorkflow.run, CheckoutInput(ev.tenant_id, ev.payload["session_id"]), id=wid,
                    task_queue=self.task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            except WorkflowAlreadyStartedError:
                return "already_started"
            return "started"
        try:
            await self.client.get_workflow_handle(wid).signal("checkout_update", ev.payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return "no_workflow"
            raise
        return "signalled"

    async def _wake_invoice(self, ev: EventEnvelope) -> str:
        try:
            await self.client.get_workflow_handle(
                invoice_workflow_id(ev.tenant_id, ev.payload["invoice_id"])).signal("invoice_update", ev.payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return "no_workflow"
            raise
        return "signalled"

    async def _start_dispute(self, ev: EventEnvelope, signal: dict[str, Any] | None) -> str:
        try:
            await self.client.start_workflow(
                DisputeWorkflow.run, DisputeInput(ev.tenant_id, ev.payload["dispute_id"]),
                id=dispute_workflow_id(ev.tenant_id, ev.payload["dispute_id"]), task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                start_signal="dispute_update" if signal else None, start_signal_args=[signal] if signal else [])
        except WorkflowAlreadyStartedError:
            return "already_started"
        return "signalled" if signal else "started"

    async def _dispute_updated(self, ev: EventEnvelope) -> str:
        update = {"status": ev.payload.get("status"), "event_id": ev.event_id}
        if update["status"] not in TERMINAL_DISPUTE:
            return await self._start_dispute(ev, update)  # signal-with-start: covers a missed dispute.opened
        handle = self.client.get_workflow_handle(dispute_workflow_id(ev.tenant_id, ev.payload["dispute_id"]))
        try:
            await handle.signal(DisputeWorkflow.dispute_update, update)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return "no_workflow"
            raise
        return "signalled"


    async def _mandate_updated(self, ev: EventEnvelope) -> str:
        """Wake the customer's open repair workflows: they re-check provider truth now instead of tomorrow."""
        def open_repairs() -> list[str]:
            with tenant_tx(ev.tenant_id, self.engine) as c:
                return [r[0] for r in c.execute(text(
                    "SELECT workflow_id FROM ops.cases WHERE tenant_id=:t AND kind='mandate' AND customer_id=:c AND "
                    "status IN ('open','waiting') AND workflow_id IS NOT NULL"),
                    {"t": ev.tenant_id, "c": ev.payload.get("customer_id")})]

        woken = 0
        for wid in await asyncio.to_thread(open_repairs):
            try:
                await self.client.get_workflow_handle(wid).signal("mandate_update", dict(ev.payload))
                woken += 1
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
        return "signalled" if woken else "no_workflow"


@dataclass
class BridgeRunner:
    """Envelope parsing, dedupe, bounded retries and dead-lettering around EventBridge.handle."""

    bridge: EventBridge
    seen: Seen
    dlq_producer: Any = None                       # nirantar.events.relay.Producer; None → DB dead-letter only
    max_attempts: int = 3
    max_backoff_s: float = 30.0
    only_tenants: frozenset[str] | None = None     # canary / sharded bridges: handle only these tenants

    async def process(self, raw: bytes, stop: asyncio.Event | None = None) -> str:
        try:
            ev = EventEnvelope.model_validate(json.loads(raw))
        except (ValidationError, json.JSONDecodeError) as exc:
            self._to_kafka_dlq(raw, f"invalid envelope: {exc}"[:500])
            self.bridge.stats["invalid"] += 1
            return "invalid"
        if self.only_tenants is not None and ev.tenant_id not in self.only_tenants:
            return "other_tenant"
        if await asyncio.to_thread(self.seen.seen, ev.event_id):
            self.bridge.stats["duplicate"] += 1
            return "duplicate"
        attempts, backoff, last = 0, 0.5, ""
        while True:
            try:
                out = await self.bridge.handle(ev)
                await asyncio.to_thread(self.seen.mark, ev.event_id)
                return out
            except Exception as exc:  # classified below: outage → wait; bad event → dead-letter
                last = f"{type(exc).__name__}: {exc}"[:1000]
                if is_transient(exc):
                    log.warning("bridge_infra_retry", event_type=ev.event_type, error=last[:200], backoff_s=backoff)
                    if stop is not None and stop.is_set():
                        raise
                else:
                    attempts += 1
                    if attempts >= self.max_attempts:
                        break
                await asyncio.sleep(backoff)
                backoff = min(self.max_backoff_s, backoff * 2)
        await asyncio.to_thread(self._dead_letter, ev, raw, last, attempts)
        self.bridge.stats["dead_lettered"] += 1
        return "dead_lettered"

    def _dead_letter(self, ev: EventEnvelope, raw: bytes, error: str, attempts: int) -> None:
        log.error("bridge_dead_letter", event_type=ev.event_type, event_id=ev.event_id, error=error[:200])
        with tenant_tx(ev.tenant_id, self.bridge.engine) as c:
            c.execute(text("INSERT INTO events.consumer_dead_letters (tenant_id, event_id, consumer, event_type, "
                           "error, envelope, attempts) VALUES (:t, :e, :c, :et, :err, CAST(:env AS jsonb), :a) "
                           "ON CONFLICT (tenant_id, consumer, event_id) DO UPDATE SET error=EXCLUDED.error, "
                           "attempts=events.consumer_dead_letters.attempts + EXCLUDED.attempts"),
                      {"t": ev.tenant_id, "e": ev.event_id, "c": CONSUMER, "et": ev.event_type, "err": error,
                       "env": raw.decode(), "a": attempts})
        self._to_kafka_dlq(raw, error)

    def _to_kafka_dlq(self, raw: bytes, error: str) -> None:
        if self.dlq_producer is None:
            log.error("bridge_invalid_event", error=error[:200])
            return
        self.dlq_producer.produce(DLQ_TOPIC, key=CONSUMER.encode(), value=raw,
                                  headers=[("consumer", CONSUMER.encode()), ("error", error[:500].encode())])
        self.dlq_producer.flush(10.0)

    async def run_kafka(self, stop: asyncio.Event, bootstrap: str | None = None,
                        group_id: str = "nirantar-event-bridge", heartbeat: Any = None) -> None:
        from confluent_kafka import Consumer

        consumer = Consumer({"bootstrap.servers": bootstrap or os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092"),
                             "group.id": group_id, "enable.auto.commit": False,
                             # a NEW consumer group starts here; afterwards committed offsets rule. Production keeps
                             # "earliest" (no event lost); a dev box full of test tenants may start at "latest".
                             "auto.offset.reset": os.environ.get("NIRANTAR_BRIDGE_OFFSET_RESET", "earliest")})
        consumer.subscribe([kafka_topic(t) for t in TOPICS])
        try:
            while not stop.is_set():
                if heartbeat:
                    heartbeat()
                msg = await asyncio.to_thread(consumer.poll, 1.0)
                if msg is None:
                    continue
                err, raw = msg.error(), msg.value()
                if err is not None:
                    if not err.fatal():
                        continue                      # e.g. topic not created yet / partition EOF
                    raise RuntimeError(f"kafka consumer fatal: {err}")
                if raw is not None:
                    await self.process(raw, stop)
                await asyncio.to_thread(consumer.commit, message=msg, asynchronous=False)
        finally:
            await asyncio.to_thread(consumer.close)
