"""Mandates from provider truth + MandateHealth workflows (P5, ADR-0015) on Postgres and a Temporal test server."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.mandates.service import mandate_id_for
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.providers.resolver import ProviderResolver
from nirantar.services.bridge import EventBridge
from nirantar.services.worker import WORKFLOWS, WorkerDeps, activities
from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.mandate import (
    MandateRepairWorkflow,
    MandateSweepInput,
    MandateSweepWorkflow,
    repair_workflow_id,
)
from tests.replay.capture import capture

pytestmark = pytest.mark.integration


def _midday_zone() -> str:
    """An IANA zone where the local time is around noon now (Etc/GMT signs are inverted: Etc/GMT-5 = UTC+5)."""
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def _merchant(engine: Engine, mock: MockProvider, *, valid_until: datetime | None = None) -> dict[str, Any]:
    t, now = new_id("ten"), datetime.now(UTC)
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, "Chai Club", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_md")
        cust = create_customer(c, t, NewCustomer(new_id("ext"), "Priya", "+919876543210", "priya@example.com", "en",
                                                 consents={"whatsapp": True, "sms": True}))
        # the repair flow is under test, not contact hours: put the customer where it is midday right now
        c.execute(text("UPDATE billing.customers SET timezone=:z WHERE customer_id=:c"),
                  {"z": _midday_zone(), "c": cust})
    token = mock.add_mandate(cust, max_amount=Money.of("2000"), valid_until=valid_until)
    psub = mock.add_subscription(cust, Money.of("999"), token_ref=token)
    with tenant_tx(t, engine) as c:
        sub = create_subscription(c, t, cust, "mock", psub, Money.of("999"))
        schedule_debit(c, t, sub, (now - timedelta(days=30)).date(), now - timedelta(days=33))
    # the provider debits last month's cycle: the payment reveals the mandate (token) behind the subscription
    pay = mock.charge(psub, succeed=True, at=now - timedelta(days=30))
    h, b = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(pay))
    assert process_raw_event(engine, mock, t, str(ingest_webhook(engine, mock, t, h, b).raw_event_id)).status \
        == "processed"
    with tenant_tx(t, engine) as c:
        schedule_debit(c, t, sub, (now + timedelta(days=5)).date(), now)            # next cycle, in 5 days
    return {"tenant": t, "customer": cust, "token": token, "subscription": sub,
            "mandate_id": mandate_id_for(t, "mock", token)}


def _token_webhook(engine: Engine, mock: MockProvider, t: str, token: str, event: str) -> str:
    h, b = mock.webhook_for(event, "token", MockProvider.token_entity(mock.mandates[token]))
    out = process_raw_event(engine, mock, t, str(ingest_webhook(engine, mock, t, h, b).raw_event_id))
    return out.detail


def test_mandate_discovered_from_payment_and_kept_true_by_token_webhooks(app_engine: Engine) -> None:
    mock = MockProvider(webhook_secret="whsec_md")
    m = _merchant(app_engine, mock)
    t = m["tenant"]
    with tenant_tx(t, app_engine) as c:
        row = c.execute(text("SELECT status, rail, max_amount_minor, provider_customer_ref FROM billing.mandates")).one()
        assert (row.status, row.rail, row.max_amount_minor, row.provider_customer_ref) == \
            ("active", "upi_autopay", 200000, m["customer"])
        assert c.execute(text("SELECT mandate_id FROM billing.subscriptions")).scalar_one() == m["mandate_id"]

    mock.set_mandate(m["token"], "paused")
    assert _token_webhook(app_engine, mock, t, m["token"], "token.paused") == "changed"
    # a forged/stale "confirmed" webhook cannot make it healthier than the provider's list says
    h, b = mock.webhook_for("token.confirmed", "token",
                            {**MockProvider.token_entity(mock.mandates[m["token"]]),
                             "recurring_details": {"status": "confirmed"}})
    process_raw_event(app_engine, mock, t, str(ingest_webhook(app_engine, mock, t, h, b).raw_event_id))
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status FROM billing.mandates")).scalar_one() == "paused"
    # rejected tokens drop out of the provider's list: the end state is accepted from the webhook
    mock.set_mandate(m["token"], "failed", "rejected_by_bank")
    assert _token_webhook(app_engine, mock, t, m["token"], "token.rejected") == "changed"
    other = mock.add_mandate("someone_else")
    assert _token_webhook(app_engine, mock, t, other, "token.confirmed") == "unknown_token"
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status, failure_reason FROM billing.mandates")).one() == \
            ("failed", "rejected_by_bank")
        events = [r[0] for r in c.execute(text("SELECT envelope->'payload'->>'status' FROM events.outbox WHERE "
                                               "event_type='mandate.updated' ORDER BY created_at"))]
    assert events == ["active", "paused", "failed"]


async def _wait_stage(handle: Any, stage: str, timeout_s: float = 60) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while (await handle.query(MandateRepairWorkflow.status))["stage"] != stage:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"workflow never reached {stage}")
        await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_mandate_repairs_resume_reauthorise_and_fraud(app_engine: Engine) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    mock, sink = MockProvider(webhook_secret="whsec_md"), MockCommsSink()
    paused = _merchant(app_engine, mock)
    expiring = _merchant(app_engine, mock, valid_until=datetime.now(UTC) + timedelta(days=12))
    fraud = _merchant(app_engine, mock)
    mock.set_mandate(paused["token"], "paused")
    _token_webhook(app_engine, mock, paused["tenant"], paused["token"], "token.paused")
    mock.set_mandate(fraud["token"], "revoked", "fraud")
    _token_webhook(app_engine, mock, fraud["tenant"], fraud["token"], "token.cancelled")

    resolver = ProviderResolver(app_engine, injected={"mock": mock})
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=WORKFLOWS,
                          activities=activities(WorkerDeps(app_engine, resolver, sink)),
                          activity_executor=ThreadPoolExecutor(8)):
            tenants = [paused["tenant"], expiring["tenant"], fraud["tenant"]]
            with env.auto_time_skipping_disabled():
                swh = await env.client.start_workflow(MandateSweepWorkflow.run, MandateSweepInput(tenants),
                                                      id=new_id("swp"), task_queue=TASK_QUEUE)
                first = await swh.result()
                await capture(swh, "MandateSweepWorkflow")
                assert first == {"tenants": 3, "started": 3, "already_handled": 0}
                again = await env.client.execute_workflow(MandateSweepWorkflow.run, MandateSweepInput(tenants),
                                                          id=new_id("swp"), task_queue=TASK_QUEUE)
                assert again["started"] == 0 and again["already_handled"] == 3    # one attempt per problem

            def handle(m: dict[str, Any]) -> Any:
                with tenant_tx(m["tenant"], app_engine) as c:
                    wid = c.execute(text("SELECT workflow_id FROM ops.cases WHERE kind='mandate'")).scalar_one()
                assert wid.startswith(repair_workflow_id(m["tenant"], m["mandate_id"], ""))
                return env.client.get_workflow_handle(wid)

            # fraud revocation: never nudged, a human decides
            assert (await handle(fraud).result())["outcome"] == "escalated"

            # paused UPI AutoPay: "resume in your UPI app" → the customer resumes → token webhook → bridge wakes it
            hp = handle(paused)
            with env.auto_time_skipping_disabled():
                await _wait_stage(hp, "waiting_customer")
                mock.set_mandate(paused["token"], "active")
                _token_webhook(app_engine, mock, paused["tenant"], paused["token"], "token.confirmed")
                with tenant_tx(paused["tenant"], app_engine) as c:
                    env_row = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='mandate.updated' "
                                             "ORDER BY created_at DESC LIMIT 1")).scalar_one()
                ev = EventEnvelope.model_validate(env_row if isinstance(env_row, dict) else json.loads(env_row))
                assert await EventBridge(app_engine, env.client, resolver).handle(ev) == "signalled"
                assert (await hp.result())["outcome"] == "repaired"
                await capture(hp, "MandateRepairWorkflow")

            # validity ends before the next debit: a re-authorisation link; the customer authorises a NEW mandate
            he = handle(expiring)
            with env.auto_time_skipping_disabled():
                await _wait_stage(he, "waiting_customer")
            (link_id, reg), = mock.registrations.items()
            assert reg["has_contact"] and reg["max_amount"] == Money.of("2000")
            new_token, auth = mock.complete_registration(link_id)
            with env.auto_time_skipping_disabled():
                h, b = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(auth))
                t_exp = expiring["tenant"]
                process_raw_event(app_engine, mock, t_exp, str(ingest_webhook(app_engine, mock, t_exp, h, b).raw_event_id))
                with tenant_tx(t_exp, app_engine) as c:
                    env_row = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='mandate.updated' "
                                             "ORDER BY created_at DESC LIMIT 1")).scalar_one()
                ev = EventEnvelope.model_validate(env_row if isinstance(env_row, dict) else json.loads(env_row))
                assert ev.payload["status"] == "active" and ev.subject_id == mandate_id_for(t_exp, "mock", new_token)
                assert await EventBridge(app_engine, env.client, resolver).handle(ev) == "signalled"
                assert (await he.result())["outcome"] == "repaired"
            with tenant_tx(expiring["tenant"], app_engine) as c:
                assert c.execute(text("SELECT mandate_id FROM billing.subscriptions")).scalar_one() == \
                    mandate_id_for(expiring["tenant"], "mock", new_token)
                assert c.execute(text("SELECT value->>'value' FROM ai.labels WHERE label_name='mandate_repaired'")
                                 ).scalar_one() == "true"

            texts = {m.to_ref: m.text for m in sink.messages}
            assert "resume it in your UPI app" in texts[paused["customer"]]
            assert f"https://mock.pay/r/{link_id}" in texts[expiring["customer"]]
            assert fraud["customer"] not in texts
            assert f"{datetime.now(UTC).date() + timedelta(days=5):%d %b %Y}" in texts[paused["customer"]]
    finally:
        await env.shutdown()
