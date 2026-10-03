from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from nirantar.contracts.events import EventEnvelope, make_event
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.db.stores import Outbox
from nirantar.events import kafka_topic
from nirantar.events.consumer import ProcessResult, SeenStore, process_message
from nirantar.events.relay import KafkaProducer, relay_engine, relay_once

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)


def _kafka_up() -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        AdminClient({"bootstrap.servers": "localhost:19092"}).list_topics(timeout=3)
        return True
    except Exception:
        return False


def test_outbox_relay_publishes_exactly_the_committed_events(app_engine: Engine) -> None:
    if not _kafka_up():
        pytest.skip("Redpanda not reachable")
    from confluent_kafka import Consumer

    tenant = new_id("ten")
    evt = make_event(
        event_type="payment.failed", version=1, tenant_id=tenant, subject_id=new_id("pay"),
        payload={"error_code": "BAD_REQUEST_ERROR"}, source="test", occurred_at=NOW,
    )
    with tenant_tx(tenant, app_engine) as c:
        c.execute(text("INSERT INTO core.tenants (tenant_id, name) VALUES (:t, 'x')"), {"t": tenant})
        Outbox(c).add(evt)

    # a rolled-back transaction must never reach Kafka
    ghost = make_event(
        event_type="payment.failed", version=1, tenant_id=tenant, subject_id="pay_ghost",
        payload={}, source="test", occurred_at=NOW,
    )
    with pytest.raises(RuntimeError), tenant_tx(tenant, app_engine) as c:
        Outbox(c).add(ghost)
        raise RuntimeError("simulated failure after state change")

    consumer = Consumer(
        {"bootstrap.servers": "localhost:19092", "group.id": new_id("grp"), "auto.offset.reset": "earliest"}
    )
    consumer.subscribe([kafka_topic("payment")])
    eng = relay_engine()
    producer = KafkaProducer()
    # Other tests/demo seeding may have left a backlog: drain until OUR event is marked published.
    for _ in range(500):
        relay_once(eng, producer, batch_size=500)
        with eng.begin() as c:
            done = c.execute(text("SELECT published_at IS NOT NULL FROM events.outbox WHERE event_id = :e"),
                             {"e": evt.event_id}).scalar_one()
        if done:
            break
    assert done, "relay never published the committed event"

    found: dict[str, EventEnvelope] = {}
    deadline = time.time() + 120
    while time.time() < deadline and evt.event_id not in found:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        env = EventEnvelope.model_validate(json.loads(msg.value()))
        found[env.event_id] = env
    consumer.close()
    assert evt.event_id in found
    assert found[evt.event_id].payload_sha256 == evt.payload_sha256
    assert ghost.event_id not in found

    with eng.begin() as c:
        pending = c.execute(
            text("SELECT count(*) FROM events.outbox WHERE event_id = :e AND published_at IS NULL"),
            {"e": evt.event_id},
        ).scalar_one()
    assert pending == 0


def test_consumer_dedupes_and_dead_letters() -> None:
    evt = make_event(
        event_type="payment.captured", version=1, tenant_id="ten_alpha", subject_id="pay_1",
        payload={"amount_minor": 99900}, source="test", occurred_at=NOW,
    )
    raw = evt.model_dump_json().encode()
    calls: list[str] = []
    seen, result = SeenStore(), ProcessResult()
    process_message(raw, lambda e: calls.append(e.event_id), seen, result)
    process_message(raw, lambda e: calls.append(e.event_id), seen, result)
    assert calls == [evt.event_id] and result.duplicates == 1

    def boom(_: EventEnvelope) -> None:
        raise ValueError("downstream failure")

    other = make_event(
        event_type="payment.captured", version=1, tenant_id="ten_alpha", subject_id="pay_2",
        payload={}, source="test", occurred_at=NOW,
    )
    process_message(other.model_dump_json().encode(), boom, seen, result)
    process_message(b"{not json", boom, seen, result)
    tampered = json.loads(raw)
    tampered["event_id"] = "evt_x"
    tampered["payload"] = {"amount_minor": 1}
    process_message(json.dumps(tampered).encode(), boom, seen, result)
    assert len(result.dead_lettered) == 3
    assert "downstream failure" in result.dead_lettered[0][1]
