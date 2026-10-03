from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nirantar.audit.chain import AuditChain, InMemoryAuditStore
from nirantar.contracts.events import EventEnvelope, make_event
from nirantar.core.clock import FixedClock
from nirantar.core.errors import AuditChainBroken, TenantContextMissing, TenantIsolationError
from nirantar.core.ids import is_valid_id, new_id
from nirantar.core.tenancy import assert_same_tenant, current_tenant, scoped_key, tenant_scope

T1, T2 = "ten_alpha", "ten_beta"
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def test_ids_are_prefixed_valid_and_time_sortable() -> None:
    a = new_id("pay", now_ms=1_000)
    b = new_id("pay", now_ms=2_000)
    assert is_valid_id(a, "pay") and a < b


def test_tenant_scope_required_and_enforced() -> None:
    with pytest.raises(TenantContextMissing):
        current_tenant()
    with tenant_scope(T1):
        assert scoped_key("idem", "abc") == "ten_alpha:idem:abc"
        with pytest.raises(TenantIsolationError):
            assert_same_tenant(T2)
        with pytest.raises(TenantIsolationError), tenant_scope(T2):
            pass


def test_invalid_tenant_id_rejected() -> None:
    with pytest.raises(TenantIsolationError), tenant_scope("Robert'); DROP TABLE"):
        pass


def test_audit_chain_detects_tampering() -> None:
    store = InMemoryAuditStore()
    chain = AuditChain(store, FixedClock(NOW))
    for i in range(5):
        chain.append(T1, "agent:verifier", "tool.call", {"i": i})
    chain.append(T2, "agent:verifier", "tool.call", {"i": 0})
    assert chain.verify(T1) == 5
    assert chain.verify(T2) == 1

    # alter a record's data hash in place
    records = store._records[T1]
    records[2] = replace(records[2], data_hash="f" * 64)
    with pytest.raises(AuditChainBroken):
        chain.verify(T1)


def test_audit_chain_detects_deletion() -> None:
    store = InMemoryAuditStore()
    chain = AuditChain(store, FixedClock(NOW))
    for i in range(3):
        chain.append(T1, "system", "x", {"i": i})
    del store._records[T1][1]
    with pytest.raises(AuditChainBroken):
        chain.verify(T1)


def test_event_envelope_is_immutable_and_hash_checked() -> None:
    evt = make_event(
        event_type="payment.failed",
        version=1,
        tenant_id=T1,
        subject_id="pay_x",
        payload={"code": "BAD_REQUEST_ERROR", "amount_minor": 99900},
        source="test",
        occurred_at=NOW,
    )
    assert evt.topic == "payment"
    assert evt.partition_key == "ten_alpha:pay_x"
    with pytest.raises(ValidationError):
        evt.payload_sha256 = "x"  # type: ignore[misc]
    tampered = evt.model_dump()
    tampered["payload"] = {"code": "OK"}
    with pytest.raises(ValueError):
        EventEnvelope(**tampered)


def test_event_rejects_unknown_topic_and_naive_time() -> None:
    with pytest.raises(ValueError):
        make_event(
            event_type="random.thing", version=1, tenant_id=T1, subject_id="s",
            payload={}, source="t", occurred_at=NOW,
        )
    with pytest.raises(ValueError):
        make_event(
            event_type="payment.failed", version=1, tenant_id=T1, subject_id="s",
            payload={}, source="t", occurred_at=datetime(2026, 1, 1),  # noqa: DTZ001
        )
