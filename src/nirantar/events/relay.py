"""Outbox → Kafka relay (at-least-once).

Rows are claimed with FOR UPDATE SKIP LOCKED so several relays can run safely.
A row is marked published only after the broker acknowledges it. Consumers are
idempotent on event_id, so a crash between ack and mark only causes a harmless
duplicate delivery.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, text

from nirantar.events import kafka_topic

DEFAULT_RELAY_URL = "postgresql+psycopg://nirantar_relay:nirantar_relay@localhost:25432/nirantar"


Headers = list[tuple[str, str | bytes | None]]


class Producer(Protocol):
    def produce(self, topic: str, key: bytes, value: bytes, headers: Headers) -> None: ...
    def flush(self, timeout: float) -> int: ...


class KafkaProducer:
    def __init__(self, bootstrap: str | None = None) -> None:
        from confluent_kafka import Producer as CProducer

        self._p = CProducer(
            {
                "bootstrap.servers": bootstrap or os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092"),
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 5,
            }
        )
        self._errors: list[str] = []

    def _cb(self, err: Any, _msg: Any) -> None:
        if err is not None:
            self._errors.append(str(err))

    def produce(self, topic: str, key: bytes, value: bytes, headers: Headers) -> None:
        self._p.produce(topic, key=key, value=value, headers=headers, on_delivery=self._cb)

    def flush(self, timeout: float) -> int:
        remaining = int(self._p.flush(timeout))
        if self._errors:
            errors, self._errors = self._errors, []
            raise RuntimeError(f"kafka delivery failed: {errors[:3]}")
        return remaining


def relay_once(engine: Engine, producer: Producer, batch_size: int = 200) -> int:
    """Publish one batch. Returns the number of events published."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tenant_id, event_id, event_type, subject_id, envelope FROM events.outbox "
                "WHERE published_at IS NULL ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": batch_size},
        ).all()
        if not rows:
            return 0
        for r in rows:
            envelope = r.envelope if isinstance(r.envelope, dict) else json.loads(r.envelope)
            producer.produce(
                kafka_topic(r.event_type.split(".", 1)[0]),
                key=f"{r.tenant_id}:{r.subject_id}".encode(),
                value=json.dumps(envelope, separators=(",", ":")).encode(),
                headers=[
                    ("event_type", r.event_type.encode()),
                    ("tenant_id", r.tenant_id.encode()),
                    ("event_id", r.event_id.encode()),
                ],
            )
        if producer.flush(10.0) != 0:
            raise RuntimeError("kafka flush timed out; batch will be retried")
        conn.execute(
            text(
                "UPDATE events.outbox SET published_at = now() "
                "WHERE (tenant_id, event_id) IN (SELECT unnest(CAST(:t AS text[])), unnest(CAST(:e AS text[])))"
            ),
            {"t": [r.tenant_id for r in rows], "e": [r.event_id for r in rows]},
        )
        return len(rows)


def relay_engine() -> Engine:
    return create_engine(os.environ.get("RELAY_DATABASE_URL", DEFAULT_RELAY_URL), pool_pre_ping=True)


def run_forever(idle_sleep_s: float = 0.5, max_backoff_s: float = 30.0) -> None:
    """Long-running relay process. Drains the outbox continuously; backs off exponentially on broker/DB errors.
    Run: uv run python -m nirantar.events.relay"""
    import time

    import structlog

    log = structlog.get_logger("outbox-relay")
    engine, producer, backoff = relay_engine(), KafkaProducer(), 1.0
    while True:
        try:
            n = relay_once(engine, producer, batch_size=500)
            backoff = 1.0
            if n == 0:
                time.sleep(idle_sleep_s)
        except Exception as exc:  # keep the relay alive; the batch is retried (at-least-once)
            log.warning("relay_error", error=str(exc)[:300], backoff_s=backoff)
            time.sleep(backoff)
            backoff = min(max_backoff_s, backoff * 2)


if __name__ == "__main__":
    run_forever()
