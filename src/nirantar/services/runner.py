"""One process for the always-on services: Temporal worker + outbox relay + event bridge + health endpoint.

    uv run python -m nirantar.services            (health: http://127.0.0.1:18090/health)

Every component reports a heartbeat; /health is 200 only while all of them are alive. A component that crashes is
restarted with backoff (the others keep running); the error and restart count show in /health.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import Engine, create_engine

log = structlog.get_logger("services")
STALE_AFTER_S = 60.0


@dataclass
class Component:
    name: str
    started_at: float = field(default_factory=time.monotonic)
    last_beat: float = field(default_factory=time.monotonic)
    restarts: int = 0
    last_error: str | None = None
    running: bool = False

    def beat(self) -> None:
        self.last_beat = time.monotonic()

    def alive(self) -> bool:
        return self.running and time.monotonic() - self.last_beat < STALE_AFTER_S

    def report(self) -> dict[str, Any]:
        return {"alive": self.alive(), "seconds_since_heartbeat": round(time.monotonic() - self.last_beat, 1),
                "restarts": self.restarts, "last_error": self.last_error}


@dataclass
class Services:
    components: dict[str, Component] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def component(self, name: str) -> Component:
        return self.components.setdefault(name, Component(name))

    def health(self) -> tuple[bool, dict[str, Any]]:
        ok = bool(self.components) and all(c.alive() for c in self.components.values())
        return ok, {"status": "ok" if ok else "degraded",
                    "components": {n: c.report() for n, c in self.components.items()},
                    "stats": {k: dict(v) if hasattr(v, "items") else v for k, v in self.stats.items()}}


async def supervise(svc: Services, name: str, body: Callable[[Component], Awaitable[None]],
                    stop: asyncio.Event) -> None:
    comp, backoff = svc.component(name), 1.0
    while not stop.is_set():
        comp.running = True
        comp.beat()
        try:
            await body(comp)
            if stop.is_set():
                break
            raise RuntimeError("component exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # restart this component; the others keep running
            comp.running, comp.restarts = False, comp.restarts + 1
            comp.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("component_restart", component=name, error=comp.last_error, backoff_s=backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(60.0, backoff * 2)
    comp.running = False


async def relay_loop(comp: Component, stop: asyncio.Event, engine: Engine, producer: Any, stats: dict[str, Any],
                     idle_s: float = 0.5) -> None:
    from nirantar.events.relay import relay_once

    stats.setdefault("published", 0)
    while not stop.is_set():
        comp.beat()
        n = await asyncio.to_thread(relay_once, engine, producer, 500)
        stats["published"] += n
        if n == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_s)
            except TimeoutError:
                pass


async def worker_loop(comp: Component, stop: asyncio.Event, worker: Any) -> None:
    async def beat() -> None:
        while not stop.is_set():
            comp.beat()
            await asyncio.sleep(5)

    task = asyncio.create_task(worker.run())
    beats, stopping = asyncio.create_task(beat()), asyncio.create_task(stop.wait())
    await asyncio.wait([task, stopping], return_when=asyncio.FIRST_COMPLETED)
    beats.cancel()
    stopping.cancel()
    if task.done():
        task.result()                         # surfaces the worker's exception to the supervisor
    else:
        await worker.shutdown()


def health_app(svc: Services) -> Any:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="nirantar-services", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> JSONResponse:
        ok, body = svc.health()
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


async def main() -> None:
    import uvicorn
    from temporalio.client import Client

    from nirantar.events.relay import KafkaProducer, relay_engine
    from nirantar.features.online import OnlineStore
    from nirantar.llm.gateway import LLMGateway
    from nirantar.ml.router import ModelRouter
    from nirantar.payments.providers.resolver import ProviderResolver
    from nirantar.services.bridge import BridgeRunner, EventBridge, RedisSeen
    from nirantar.services.worker import (
        WorkerDeps,
        build_worker,
        ensure_billing_schedule,
        ensure_health_schedule,
        ensure_mandate_schedule,
        ensure_sweep_schedule,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:          # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    env = os.environ.get("NIRANTAR_ENV", "local")
    engine = create_engine(os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://nirantar_app:nirantar_app@localhost:25432/nirantar"), pool_pre_ping=True,
        pool_size=20)
    resolver = ProviderResolver(engine)
    from nirantar.approvals.executor import default_comms

    comms = default_comms(engine)
    llm = LLMGateway.from_env()
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    from sqlalchemy import create_engine as _create_engine

    owner = _create_engine(os.environ.get(
        "DATABASE_OWNER_URL", "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"),
        pool_pre_ping=True)
    deps = WorkerDeps(engine, resolver, comms, llm if llm.providers else None, OnlineStore(), ModelRouter(),
                      environment=env, owner=owner)
    log.info("services_starting", environment=env, comms=type(comms).__name__,
             reconciliation=await ensure_sweep_schedule(client), mandate_health=await ensure_mandate_schedule(client),
             billing=await ensure_billing_schedule(client), health=await ensure_health_schedule(client))

    svc = Services()
    bridge = EventBridge(engine, client, resolver)
    only = frozenset(x for x in os.environ.get("NIRANTAR_BRIDGE_TENANTS", "").split(",") if x) or None
    runner = BridgeRunner(bridge, RedisSeen(), KafkaProducer(), only_tenants=only)
    svc.stats = {"bridge": bridge.stats, "relay": {}}
    relay_eng, relay_producer = relay_engine(), KafkaProducer()

    async def run_worker(c: Component) -> None:
        await worker_loop(c, stop, build_worker(client, deps))

    async def run_relay(c: Component) -> None:
        await relay_loop(c, stop, relay_eng, relay_producer, svc.stats["relay"])

    async def run_bridge(c: Component) -> None:
        await runner.run_kafka(stop, heartbeat=c.beat)

    server = uvicorn.Server(uvicorn.Config(health_app(svc), host="127.0.0.1",
                                           port=int(os.environ.get("NIRANTAR_SERVICES_PORT", "18090")),
                                           log_level="warning"))
    tasks = [asyncio.create_task(supervise(svc, "worker", run_worker, stop)),
             asyncio.create_task(supervise(svc, "relay", run_relay, stop)),
             asyncio.create_task(supervise(svc, "event_bridge", run_bridge, stop)),
             asyncio.create_task(server.serve())]
    try:
        await stop.wait()
    finally:
        stop.set()
        server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()
        relay_eng.dispose()
