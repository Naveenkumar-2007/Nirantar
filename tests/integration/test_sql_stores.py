from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from nirantar.audit.chain import AuditChain
from nirantar.contracts.events import make_event
from nirantar.core.errors import IdempotencyConflict
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.db.stores import IdempotencyStore, Outbox, SqlAuditStore, SqlLedgerStore
from nirantar.ledger import Account, AccountType, Ledger, Line

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)


@pytest.fixture
def tenant(app_engine: Engine) -> str:
    t = new_id("ten")
    with tenant_tx(t, app_engine) as c:
        c.execute(text("INSERT INTO core.tenants (tenant_id, name) VALUES (:t, 'x')"), {"t": t})
    return t


def test_sql_ledger_matches_in_memory_semantics(app_engine: Engine, tenant: str) -> None:
    with tenant_tx(tenant, app_engine) as c:
        ledger = Ledger(SqlLedgerStore(c))
        ledger.open_account(Account(tenant, "receivable:subscriptions", AccountType.ASSET))
        ledger.open_account(Account(tenant, "income:subscriptions", AccountType.INCOME))
        lines = [
            Line("receivable:subscriptions", debit=Money.of("999")),
            Line("income:subscriptions", credit=Money.of("999")),
        ]
        e1 = ledger.post(tenant_id=tenant, idempotency_key="bill-1", lines=lines, memo="b", effective_at=NOW)
        e2 = ledger.post(tenant_id=tenant, idempotency_key="bill-1", lines=lines, memo="b", effective_at=NOW)
        assert e1.entry_id == e2.entry_id
        with pytest.raises(IdempotencyConflict):
            ledger.post(
                tenant_id=tenant, idempotency_key="bill-1", memo="b", effective_at=NOW,
                lines=[
                    Line("receivable:subscriptions", debit=Money.of("1")),
                    Line("income:subscriptions", credit=Money.of("1")),
                ],
            )
        assert ledger.balance(tenant, "receivable:subscriptions") == Money.of("999")
        ledger.reverse(tenant_id=tenant, entry_id=e1.entry_id, idempotency_key="rev-1", reason="test")
        assert ledger.balance(tenant, "receivable:subscriptions").is_zero
        assert ledger.trial_balance_ok(tenant)


def test_sql_audit_chain_verifies(app_engine: Engine, tenant: str) -> None:
    with tenant_tx(tenant, app_engine) as c:
        chain = AuditChain(SqlAuditStore(c))
        for i in range(4):
            chain.append(tenant, "system:test", "tool.call", {"i": i})
        assert chain.verify(tenant) == 4


def test_outbox_and_idempotency(app_engine: Engine, tenant: str) -> None:
    evt = make_event(
        event_type="payment.failed", version=1, tenant_id=tenant, subject_id="pay_1",
        payload={"code": "X"}, source="test", occurred_at=NOW,
    )
    with tenant_tx(tenant, app_engine) as c:
        Outbox(c).add(evt)
        Outbox(c).add(evt)  # idempotent
        assert c.execute(text("SELECT count(*) FROM events.outbox")).scalar_one() == 1

        idem = IdempotencyStore(c)
        assert idem.begin(tenant, "refund", "k1", {"amount": 100}) is None
        assert idem.begin(tenant, "refund", "k1", {"amount": 100}) == {"status": "in_progress"}
        idem.complete(tenant, "refund", "k1", {"refund_id": "rf_1"})
        assert idem.begin(tenant, "refund", "k1", {"amount": 100}) == {"refund_id": "rf_1"}
        with pytest.raises(IdempotencyConflict):
            idem.begin(tenant, "refund", "k1", {"amount": 999})
