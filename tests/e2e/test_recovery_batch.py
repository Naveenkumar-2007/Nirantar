"""Recovery Command Centre end to end (P8.5, ADR-0021) — the brief's bar on a real pipeline:

declined debits arrive as signed provider webhooks → the queue ranks them → the operator previews every message and
its compliance decision → launches a batch with a randomised holdout → RecoveryBatchWorkflow sends payment links
through the MCP gateway (consent enforced: no-consent customers are refused) → customers pay → the proof report
shows verified money recovered, treatment vs holdout, every action, and an intact audit chain. Plus: the stop
button, and a person replying from the inbox through the same gateway.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.comms.sink import MockCommsSink
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.recovery import batch
from nirantar.security.keys import create_principal, issue_api_key
from nirantar.workflows.recovery import RecoveryBatchWorkflow, batch_workflow_id
from nirantar.workflows.recovery_activities import RecoveryActivities, RecoveryDeps
from tests.replay.capture import capture

pytestmark = pytest.mark.integration
QUEUE = "recovery-batch-test"
N, NO_CONSENT = 40, 4


def _midday_zone() -> str:
    """An IANA zone where it is around noon now, so contact windows are open (Etc/GMT signs are inverted)."""
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def _deliver(engine: Engine, mock: MockProvider, tenant: str, kind: str, payment: Any) -> None:
    headers, body = mock.webhook_for(kind, "payment", MockProvider.payment_entity(payment))
    res = ingest_webhook(engine, mock, tenant, headers, body)
    process_raw_event(engine, mock, tenant, str(res.raw_event_id))


@pytest.fixture()
def merchant(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    """40 subscribers whose ₹499 debit was declined today (real webhook pipeline); 4 never gave WhatsApp consent."""
    tenant, mock, sink, zone = new_id("ten"), MockProvider(webhook_secret="whsec_rb"), MockCommsSink(), _midday_zone()
    subs: dict[str, str] = {}
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Recovery merchant (synthetic)", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_rb")
        for i in range(N):
            cust = create_customer(c, tenant, NewCustomer(f"rb{i}", f"Cust {i}", f"+9190001{i:05d}", None,
                                                          ("en", "hi", "te")[i % 3],
                                                          consents={"whatsapp": i >= NO_CONSENT, "sms": True}))
            c.execute(text("UPDATE billing.customers SET timezone=:z WHERE customer_id=:c"), {"z": zone, "c": cust})
            psub = mock.add_subscription(cust, Money.of("499"))
            sub = create_subscription(c, tenant, cust, "mock", psub, Money.of("499"))
            schedule_debit(c, tenant, sub, datetime.now(UTC).date(), datetime.now(UTC) - timedelta(days=3))
            subs[cust] = psub
    for psub in subs.values():
        _deliver(app_engine, mock, tenant, "payment.failed",
                 mock.charge(psub, succeed=False, error_code="BAD_REQUEST_ERROR"))
    pid = create_principal(app_engine, tenant, "ops-owner", "user", ["owner"])
    key = issue_api_key(app_engine, tenant, pid).plaintext
    yield {"tenant": tenant, "mock": mock, "sink": sink, "subs": subs, "key": key,
           "no_consent": set(list(subs)[:NO_CONSENT])}


def _app(app_engine: Engine, owner_engine: Engine, m: dict[str, Any], **extra: Any) -> Any:
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": m["mock"],
                                                       "comms": m["sink"]})
    return create_app(Services(engine=app_engine, owner_engine=owner_engine,
                               extra={"recovery_gateway": gw, "operator_gateway": gw, "task_queue": QUEUE, **extra}))


@pytest.mark.asyncio
async def test_batch_recovers_verified_money_against_a_holdout(app_engine: Engine, owner_engine: Engine,
                                                               merchant: dict[str, Any]) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    m, t = merchant, merchant["tenant"]
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        app = _app(app_engine, owner_engine, m, temporal=env.client)
        h = {"Authorization": f"Bearer {m['key']}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
            # ---- the queue: every declined debit, ranked, honest about having no recovery history yet
            q = (await api.get("/v1/recovery/queue", headers=h)).json()
            failed = [x for x in q["items"] if x["kind"] == "failed_debit"]
            assert len(failed) == N and {x["state"] for x in failed} == {"stalled"}
            assert all(x["probability"] is None and x["action"] == "payment_link_whatsapp" for x in failed)
            ids = [x["item_id"] for x in failed]

            # ---- dry run: the exact message, and the Guardian refuses the customers without WhatsApp consent
            plan = (await api.post("/v1/recovery/plan", json={"item_ids": ids}, headers=h)).json()
            assert plan["counts"]["ALLOW"] == N - NO_CONSENT and plan["counts"]["DENY"] == NO_CONSENT
            assert all("₹499.00" in p["message"] and "{link}" in p["message"] for p in plan["items"])
            assert m["sink"].messages == []                                            # a preview sends nothing

            # ---- launch: 25% randomised holdout, 3-day measurement window
            async with Worker(env.client, task_queue=QUEUE, workflows=[RecoveryBatchWorkflow],
                              activities=RecoveryActivities(RecoveryDeps(app_engine, m["mock"], m["sink"])).all(),
                              activity_executor=ThreadPoolExecutor(8)):
                r = await api.post("/v1/recovery/batches", headers=h, json={
                    "item_ids": ids, "name": "Declined today", "holdout_pct": 25, "window_days": 3})
                assert r.status_code == 200, r.text
                launched = r.json()
                bid = launched["batch_id"]
                assert launched["arms"]["holdout"] > 0 and launched["arms"]["treatment"] > 0
                again = await api.post("/v1/recovery/plan", json={"item_ids": ids[:1]}, headers=h)
                assert again.status_code == 422                                       # one batch per item

                handle = env.client.get_workflow_handle(batch_workflow_id(t, bid))
                for _ in range(100):
                    if (await handle.query(RecoveryBatchWorkflow.status))["done"] == launched["arms"]["treatment"]:
                        break
                    await env.sleep(timedelta(seconds=5))
                with tenant_tx(t, app_engine) as c:
                    items = {r.customer_id: r for r in c.execute(text(
                        "SELECT customer_id, arm, state FROM ops.recovery_batch_items WHERE batch_id=:b"), {"b": bid})}
                treated = {cu for cu, r in items.items() if r.arm == "treatment"}
                holdout = {cu for cu, r in items.items() if r.arm == "holdout"}
                sent_to = {msg.to_ref for msg in m["sink"].messages}
                assert sent_to == {cu for cu in treated if cu not in m["no_consent"]}  # never holdout, never no-consent
                assert all(items[cu].state == "denied" for cu in treated & m["no_consent"])
                assert all("₹499.00" in msg.text and "https://mock.pay/" in msg.text for msg in m["sink"].messages)

                # ---- customers respond: half the messaged customers pay their link; one holdout pays by itself
                links = {lk.url: lk.link_id for lk in m["mock"].links.values()}
                payers = sorted(sent_to)[::2]
                for msg in m["sink"].messages:
                    if msg.to_ref in payers:
                        url = next(u for u in links if u in msg.text)
                        _deliver(app_engine, m["mock"], t, "payment.captured", m["mock"].pay_link(links[url]))
                self_payer = sorted(holdout)[0]
                _deliver(app_engine, m["mock"], t, "payment.captured",
                         m["mock"].charge(m["subs"][self_payer], succeed=True))

                live = (await api.get(f"/v1/recovery/batches/{bid}", headers=h)).json()
                assert live["batch"]["status"] == "running"
                assert live["recovered"]["treatment"]["value_minor"] == 49_900 * len(payers)

                await env.sleep(timedelta(days=3, hours=1))
                result = await handle.result()
                await capture(handle, "RecoveryBatchWorkflow")
            assert result["status"] == "completed"

            # ---- the proof
            report = (await api.get(f"/v1/recovery/batches/{bid}", headers=h)).json()
            assert report["batch"]["status"] == "completed" and report["audit"]["valid"]
            assert report["recovered"]["treatment"] == {"customers": len(treated), "recovered": len(payers),
                                                        "value_minor": 49_900 * len(payers)}
            assert report["recovered"]["holdout"]["recovered"] == 1
            assert set(report["statistics"]["arms"]) == {"treatment", "holdout"}
            sends = [a for a in report["actions"] if a["tool_name"] == "comms.send_whatsapp"]
            assert len([a for a in sends if a["status"] == "executed"]) == len(sent_to)
            assert all(a["policy_decision"] == "DENY" for a in sends if a["status"] == "denied")
            with tenant_tx(t, app_engine) as c:
                outcomes = c.execute(text(
                    "SELECT count(*), sum(value_minor) FROM experiments.outcomes WHERE experiment_id=:e AND verified"),
                    {"e": report["batch"]["experiment_id"]}).one()
            assert outcomes[0] == len(payers) + 1 and outcomes[1] == 49_900 * (len(payers) + 1)
            listed = (await api.get("/v1/recovery/batches", headers=h)).json()["items"]
            assert listed[0]["batch_id"] == bid and listed[0]["status"] == "completed"
    finally:
        await env.shutdown()


def test_stop_button_skips_remaining_items(app_engine: Engine, merchant: dict[str, Any]) -> None:
    m, t, now = merchant, merchant["tenant"], datetime.now(UTC)
    with tenant_tx(t, app_engine) as c:
        ids = [x["item_id"] for x in batch.queue.build(c, now)["items"] if x["customer_id"] not in m["no_consent"]][:10]
    out = batch.launch(app_engine, t, ids, name="stop me", holdout_bp=0, window_days=2, actor="user:test", now=now)
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": m["mock"],
                                                       "comms": m["sink"]})
    first, *rest = batch.treatment_items(app_engine, t, out["batch_id"])
    assert batch.execute_item(app_engine, gw, t, out["batch_id"], first, now)["state"] == "executed"
    stopped = batch.stop(app_engine, t, out["batch_id"], "user:test", now)
    assert stopped["skipped"] == len(rest)
    assert batch.execute_item(app_engine, gw, t, out["batch_id"], rest[0], now)["state"] == "stopped"
    assert len(m["sink"].messages) == 1                                         # nothing sent after the stop
    report = batch.measure_and_close(app_engine, t, out["batch_id"], now)
    assert report["batch"]["status"] == "stopped" and report["states"]["stopped"] == len(rest)
    with pytest.raises(batch.BatchError):
        batch.stop(app_engine, t, out["batch_id"], "user:test", now)


@pytest.mark.asyncio
async def test_operator_reply_goes_through_the_gateway(app_engine: Engine, owner_engine: Engine,
                                                       merchant: dict[str, Any]) -> None:
    m = merchant
    cust = sorted(set(m["subs"]) - m["no_consent"])[0]
    app = _app(app_engine, owner_engine, m)
    h = {"Authorization": f"Bearer {m['key']}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        ok = await api.post(f"/v1/conversations/{cust}/reply", headers=h,
                            json={"text": "Hi, your ₹499.00 payment can be done any time this week. Thank you!"})
        assert ok.status_code == 200, ok.text
        wrong = await api.post(f"/v1/conversations/{cust}/reply", headers=h,
                               json={"text": "Please pay ₹4,990.00 today."})
        assert wrong.status_code == 409 and "not an amount this customer owes" in wrong.text
        rude = await api.post(f"/v1/conversations/{cust}/reply", headers=h,
                              json={"text": "Pay now or we will send recovery agents to your home and inform your family."})
        assert rude.status_code == 409                                       # conduct rules apply to people too
    assert [msg.to_ref for msg in m["sink"].messages] == [cust]
    with tenant_tx(m["tenant"], app_engine) as c:
        row = c.execute(text("SELECT agent_id, params->>'operator' AS op, status FROM ops.actions WHERE "
                             "tool_name='comms.operator_reply' AND status='executed'")).one()
    assert row.agent_id == "human_operator" and row.op.startswith("user:")
