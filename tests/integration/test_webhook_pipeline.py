"""Phase 6: webhook ingress → processing → verifier → ledger, plus reconciliation and failure paths."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    mark_attempting,
    schedule_debit,
)
from nirantar.core.crypto import CryptoError, decrypt
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlLedgerStore
from nirantar.ledger import Ledger
from nirantar.payments.domain import LinkRequest, PaymentStatus, ProviderPayment
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import apply_payment, process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.reconciliation import reconcile_window
from nirantar.verifier.payments import RECEIVABLE, clearing_account

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)


@pytest.fixture
def world(app_engine: Engine) -> dict[str, object]:
    mock = MockProvider(webhook_secret="whsec_local")
    tenant = new_id("ten")
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Chai Club (D2C subscriptions)")
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_local")
        cust = create_customer(c, tenant, NewCustomer("ext-1", "Priya", "+919876543210", "priya@example.com",
                                                      preferred_language="te"))
        provider_sub = mock.add_subscription(cust, Money.of("999"))
        sub = create_subscription(c, tenant, cust, "mock", provider_sub, Money.of("999"),
                                  next_charge_on=date(2026, 10, 5))
        debit = schedule_debit(c, tenant, sub, date(2026, 10, 5), NOW)
        mark_attempting(c, tenant, debit)
    return {"mock": mock, "tenant": tenant, "customer": cust, "provider_sub": provider_sub, "debit": debit}


def _debit_status(engine: Engine, tenant: str, debit: str) -> str:
    with tenant_tx(tenant, engine) as c:
        return str(c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"), {"d": debit}).scalar_one())


def test_pii_is_encrypted_at_rest_and_bound_to_tenant(app_engine: Engine, world: dict[str, object]) -> None:
    tenant = str(world["tenant"])
    with tenant_tx(tenant, app_engine) as c:
        blob = c.execute(text("SELECT phone_enc FROM billing.customers WHERE customer_id=:c"),
                         {"c": world["customer"]}).scalar_one()
    assert b"9876543210" not in bytes(blob)
    assert decrypt(bytes(blob), tenant) == "+919876543210"
    with pytest.raises(CryptoError):
        decrypt(bytes(blob), "ten_someoneelse")


def test_failed_charge_then_recovery_link_settles_ledger(app_engine: Engine, world: dict[str, object]) -> None:
    mock: MockProvider = world["mock"]  # type: ignore[assignment]
    tenant, debit = str(world["tenant"]), str(world["debit"])

    # 1. Mandate debit fails at the provider; webhook arrives.
    failed = mock.charge(str(world["provider_sub"]), succeed=False, error_code="BAD_REQUEST_ERROR", at=NOW)
    headers, body = mock.webhook_for("payment.failed", "payment", MockProvider.payment_entity(failed), at=NOW)
    res = ingest_webhook(app_engine, mock, tenant, headers, body)
    assert res.status_code == 200 and res.raw_event_id
    # duplicate delivery is acknowledged but not stored twice
    assert ingest_webhook(app_engine, mock, tenant, headers, body).duplicate
    out = process_raw_event(app_engine, mock, tenant, res.raw_event_id)
    assert (out.event_type, out.debit_id) == ("payment.failed", debit)
    assert _debit_status(app_engine, tenant, debit) == "failed"
    assert process_raw_event(app_engine, mock, tenant, res.raw_event_id).status == "skipped"  # idempotent

    # 2. Recovery via payment link (reference ties it to the debit), customer pays.
    link = mock.create_payment_link(LinkRequest(Money.of("999"), f"{debit}.1", "Renewal", "Priya", None, None))
    paid = mock.pay_link(link.link_id, at=NOW + timedelta(days=2))
    headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(paid))
    res2 = ingest_webhook(app_engine, mock, tenant, headers, body)
    out2 = process_raw_event(app_engine, mock, tenant, str(res2.raw_event_id))
    assert out2.event_type == "payment.captured" and out2.detail == "settled"
    assert _debit_status(app_engine, tenant, debit) == "succeeded"

    # 3. Ledger: receivable accrued on scheduling, cleared by the verified capture.
    with tenant_tx(tenant, app_engine) as c:
        ledger = Ledger(SqlLedgerStore(c))
        assert ledger.balance(tenant, RECEIVABLE).is_zero
        assert ledger.balance(tenant, clearing_account("mock")) == Money.of("999")
        assert ledger.trial_balance_ok(tenant)
        kinds = c.execute(text("SELECT event_type FROM events.outbox ORDER BY created_at")).scalars().all()
    assert {"subscription.debit_scheduled", "provider.webhook_received", "payment.failed",
            "payment.captured"} <= set(kinds)


def test_spoofed_webhook_is_rejected_and_not_stored(app_engine: Engine, world: dict[str, object]) -> None:
    mock: MockProvider = world["mock"]  # type: ignore[assignment]
    tenant = str(world["tenant"])
    pay = mock.charge(str(world["provider_sub"]), succeed=True)
    headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(pay))
    headers["X-Razorpay-Signature"] = "0" * 64
    assert ingest_webhook(app_engine, mock, tenant, headers, body).status_code == 401
    assert ingest_webhook(app_engine, mock, "ten_unknowntenant", headers, body).status_code == 404
    with tenant_tx(tenant, app_engine) as c:
        assert c.execute(text("SELECT count(*) FROM ingest.provider_events")).scalar_one() == 0


def test_provider_outage_during_processing_is_retried_not_lost(app_engine: Engine, world: dict[str, object]) -> None:
    mock: MockProvider = world["mock"]  # type: ignore[assignment]
    tenant = str(world["tenant"])
    pay = mock.charge(str(world["provider_sub"]), succeed=True)
    headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(pay))
    res = ingest_webhook(app_engine, mock, tenant, headers, body)
    mock.outage = True
    assert process_raw_event(app_engine, mock, tenant, str(res.raw_event_id)).status == "retry"
    mock.outage = False
    assert process_raw_event(app_engine, mock, tenant, str(res.raw_event_id)).event_type == "payment.captured"


def test_out_of_order_regression_becomes_discrepancy(app_engine: Engine, world: dict[str, object]) -> None:
    mock: MockProvider = world["mock"]  # type: ignore[assignment]
    tenant = str(world["tenant"])
    pay = mock.charge(str(world["provider_sub"]), succeed=True)
    with tenant_tx(tenant, app_engine) as c:
        apply_payment(c, tenant, mock, pay, NOW, None, None)
        stale = ProviderPayment(pay.provider, pay.provider_payment_id, pay.amount, PaymentStatus.FAILED,
                                "upi", "X", "stale", pay.created_at, subscription_ref=pay.subscription_ref)
        apply_payment(c, tenant, mock, stale, NOW, None, None)
        status = c.execute(text("SELECT status FROM billing.payments WHERE provider_payment_id=:p"),
                           {"p": pay.provider_payment_id}).scalar_one()
        kinds = c.execute(text("SELECT kind FROM billing.discrepancies")).scalars().all()
    assert status == "captured" and kinds == ["status_regression"]


def test_reconciliation_heals_a_missed_webhook(app_engine: Engine, world: dict[str, object]) -> None:
    mock: MockProvider = world["mock"]  # type: ignore[assignment]
    tenant, debit = str(world["tenant"]), str(world["debit"])
    mock.charge(str(world["provider_sub"]), succeed=True, at=NOW)  # no webhook ever sent
    report = reconcile_window(app_engine, mock, tenant, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert report.scanned == 1 and "payment.captured" in report.events
    assert _debit_status(app_engine, tenant, debit) == "succeeded"
    again = reconcile_window(app_engine, mock, tenant, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert again.changed == 0  # idempotent
