"""Checkout drop-off recovery end to end (ADR-0028), on real Postgres + Temporal (time-skipping).

A store reports a checkout whose UPI payment failed at the bank → the workflow waits for the customer to go quiet →
diagnoses "bank issue" → sends ONE reminder worded for that cause with a link for exactly the cart amount → the
customer pays through the link → the provider confirms → booked once, checkout recovered, workflow done.
Plus: idempotent events, a store key that can do nothing else, consent gating, holdout measurement, the original
order paying on its own, and tenant isolation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.billing.service import connect_provider, create_tenant
from nirantar.checkout import service as checkouts
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.domain import PaymentStatus, ProviderPayment
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.security.keys import create_principal, issue_api_key
from nirantar.services.bridge import EventBridge
from nirantar.settings import service as settings
from nirantar.workflows.checkout import CheckoutRecoveryWorkflow, checkout_workflow_id
from nirantar.workflows.checkout_activities import CheckoutActivities, CheckoutDeps
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUEUE = "checkout-test"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def store(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    t, mock, sink = new_id("ten"), MockProvider(webhook_secret="whsec_shop"), MockCommsSink()
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Kaveri Handlooms", {"segments": ["ecommerce"], "synthetic": True})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_shop")
        settings.update(c, t, "experiments", {"holdout_bp": 0, "holdout_opt_in": False}, actor="user:test",
                        reason="recovery path under test; measurement tested separately", now=datetime.now(UTC),
                        expected_version=0)
    keys = {}
    for role in ("owner", "checkout_ingest"):
        keys[role] = issue_api_key(app_engine, t, create_principal(app_engine, t, role, "service" if role !=
                                                                   "owner" else "user", [role])).plaintext
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine, extra={"provider_override": mock}))
    yield {"tenant": t, "mock": mock, "sink": sink, "app": app,
           "owner": {"Authorization": f"Bearer {keys['owner']}"},
           "store": {"Authorization": f"Bearer {keys['checkout_ingest']}"}}


def _customer(ref: str, consents: dict[str, bool]) -> dict[str, Any]:
    return {"external_ref": ref, "name": "Meena Iyer", "phone_e164": "+919000004444", "language": "en",
            "consents": consents}


def _webhook(engine: Engine, mock: MockProvider, tenant: str, p: ProviderPayment) -> None:
    mock.payments[p.provider_payment_id] = p
    kind = "payment.captured" if p.status == PaymentStatus.CAPTURED else "payment.failed"
    headers, body = mock.webhook_for(kind, "payment", MockProvider.payment_entity(p))
    process_raw_event(engine, mock, tenant, str(ingest_webhook(engine, mock, tenant, headers, body).raw_event_id))


def _outbox(engine: Engine, tenant: str, seen: set[str]) -> list[EventEnvelope]:
    with tenant_tx(tenant, engine) as c:
        rows = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='checkout.updated' "
                              "ORDER BY created_at")).scalars().all()
    out = []
    for r in rows:
        ev = EventEnvelope.model_validate(r if isinstance(r, dict) else json.loads(r))
        if ev.event_id not in seen:
            seen.add(ev.event_id)
            out.append(ev)
    return out


async def test_failed_upi_checkout_is_recovered_once_and_booked_once(app_engine: Engine,
                                                                     store: dict[str, Any]) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    m, t = store, store["tenant"]
    now = datetime.now(UTC)
    consent = {"whatsapp": True, "promotional": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        first = {"event_id": "ev-1", "type": "initiated", "checkout_ref": "ORD-1001", "amount_rupees": "2499",
                 "items": [{"name": "Kanchipuram silk stole", "qty": 1}, {"name": "Cotton dupatta", "qty": 2}],
                 "customer": _customer("C-77", consent), "at": (now - timedelta(minutes=5)).isoformat()}
        r = await api.post("/v1/checkout/events", headers=m["store"], json=first)
        assert r.status_code == 200 and r.json()["new"] is True, r.text
        session = r.json()["session_id"]
        assert (await api.post("/v1/checkout/events", headers=m["store"], json=first)).json()["duplicate"] is True
        for ev in ({"event_id": "ev-2", "type": "payment_page"},
                   {"event_id": "ev-3", "type": "payment_failed", "failure_code": "bank_technical_error"}):
            r = await api.post("/v1/checkout/events", headers=m["store"],
                               json={**ev, "checkout_ref": "ORD-1001", "at": now.isoformat()})
            assert r.status_code == 200, r.text
        # a store key reports checkout events and can do nothing else
        assert (await api.get("/v1/overview", headers=m["store"])).status_code == 403
        assert (await api.post("/v1/invoices", headers=m["store"], json={})).status_code in (403, 422)
        # a future-dated event is refused
        bad = await api.post("/v1/checkout/events", headers=m["store"], json={
            "event_id": "ev-x", "type": "payment_page", "checkout_ref": "ORD-1001",
            "at": (now + timedelta(hours=2)).isoformat()})
        assert bad.status_code == 422

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            acts = CheckoutActivities(CheckoutDeps(app_engine, m["mock"], m["sink"]))
            async with Worker(env.client, task_queue=QUEUE, workflows=[CheckoutRecoveryWorkflow],
                              activities=acts.all(), activity_executor=ThreadPoolExecutor(4)):
                bridge = EventBridge(app_engine, env.client, m["mock"], task_queue=QUEUE)
                seen: set[str] = set()
                outcomes = [await bridge.handle(ev) for ev in _outbox(app_engine, t, seen)]
                assert outcomes[0] == "started" and set(outcomes[1:]) <= {"signalled"}
                h = env.client.get_workflow_handle(checkout_workflow_id(t, session))

                def steps() -> dict[str, str]:
                    with tenant_tx(t, app_engine) as c:
                        return dict(c.execute(text("SELECT step, status FROM ops.checkout_chases WHERE "
                                                   "session_id=:s"), {"s": session}).all())

                for _ in range(80):              # quiet period, then (whatever the wall clock) the contact window
                    if steps().get("nudge") == "executed":
                        break
                    await env.sleep(timedelta(minutes=30))
                assert steps().get("nudge") == "executed"
                sent = [(tp, msg) for tp, msg in zip(m["sink"].templates, m["sink"].messages, strict=True)
                        if tp and tp.key.startswith("whatsapp.checkout")]
                assert len(sent) == 1 and sent[0][0].key == "whatsapp.checkout_bank_issue"     # worded for the cause
                assert "₹2,499.00" in sent[0][1].text and "nothing was charged" in sent[0][1].text
                assert sent[0][1].at.astimezone(IST).hour >= 9                  # never outside the contact window

                # the customer pays through the recovery link; only the provider's word counts
                with tenant_tx(t, app_engine) as c:
                    req = c.execute(text("SELECT request_id, amount_minor FROM billing.payment_requests WHERE "
                                         "checkout_session_id=:s"), {"s": session}).one()
                assert req.amount_minor == 249_900                             # exactly the cart, never invented
                paid = ProviderPayment("mock", m["mock"]._next("pay"), Money.of("2499"), PaymentStatus.CAPTURED,
                                       "upi", None, None, datetime.now(UTC),
                                       notes={"reference_id": req.request_id})
                _webhook(app_engine, m["mock"], t, paid)
                _webhook(app_engine, m["mock"], t, paid)                       # a replayed webhook changes nothing
                for ev in _outbox(app_engine, t, seen):
                    await bridge.handle(ev)
                for _ in range(20):
                    if (await h.describe()).status.name != "RUNNING":
                        break
                    await env.sleep(timedelta(hours=1))
                assert (await h.result())["outcome"] == "paid"
                await capture(h, "CheckoutRecoveryWorkflow")
        finally:
            await env.shutdown()

        with tenant_tx(t, app_engine) as c:
            k = c.execute(text("SELECT status, paid_via, paid_minor, cause, arm FROM billing.checkout_sessions WHERE "
                               "session_id=:s"), {"s": session}).one()
            booked = c.execute(text("SELECT count(*) FROM ledger.entries WHERE memo LIKE "
                                    "'verified recovered checkout ORD-1001%'")).scalar_one()
        assert (k.status, k.paid_via, k.paid_minor, k.cause, k.arm) == ("paid", "recovery_link", 249_900,
                                                                         "bank_issue", "treatment")
        assert booked == 1
        assert len([tp for tp in m["sink"].templates if tp and tp.key.startswith("whatsapp.checkout")]) == 1
        stats = (await api.get("/v1/checkout-recovery", headers=m["owner"])).json()
        assert stats["funnel"]["recovered"] == 1 and stats["funnel"]["verified_minor"] == 249_900
        detail = (await api.get(f"/v1/checkout-sessions/{session}", headers=m["owner"])).json()
        assert [e["type"] for e in detail["events"]] == ["initiated", "payment_page", "payment_failed"]


def test_consent_original_order_holdout_and_isolation(app_engine: Engine, store: dict[str, Any]) -> None:
    m, t = store, store["tenant"]
    now = datetime.now(UTC)
    with tenant_tx(t, app_engine) as c:
        # no promotional consent: a cart reminder is refused by policy, nothing is sent
        a = checkouts.record_event(c, t, checkouts.CheckoutEvent(
            "e-a", "initiated", "ORD-2001", now - timedelta(hours=2), amount=Money.of("899"),
            customer=checkouts.CheckoutCustomer("C-81", "Arjun", "+919000005555", consents={"whatsapp": True})),
            now)
        assert checkouts.assess(c, t, a["session_id"], now, 100_00)["eligible"] is True
        # the checkout's own order is paid through the provider: closed as paid, NOT booked (the store's sale)
        b = checkouts.record_event(c, t, checkouts.CheckoutEvent(
            "e-b", "initiated", "ORD-2002", now, amount=Money.of("1500"), provider="mock",
            provider_order_id="order_ORIG2002"), now)
        # below the minimum: observed, never contacted
        low = checkouts.record_event(c, t, checkouts.CheckoutEvent(
            "e-c", "initiated", "ORD-2003", now, amount=Money.of("49"),
            customer=checkouts.CheckoutCustomer("C-82", "Ravi", "+919000006666",
                                                consents={"whatsapp": True, "promotional": True})), now)
        assert checkouts.assess(c, t, low["session_id"], now, 100_00)["eligible"] is False
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": m["mock"], "comms": m["sink"]})
    r = gw.call(tenant_id=t, agent_id="checkout_agent", tool_name="billing.send_checkout_recovery",
                args={"session_id": a["session_id"], "step": "nudge"})
    assert r.status == "denied" and m["sink"].messages == []
    assert any("promotional" in h.message for h in r.decision.hits)          # type: ignore[union-attr]
    # agents outside the checkout scope cannot use the tool at all
    with pytest.raises(Exception, match="not allowed"):
        gw.call(tenant_id=t, agent_id="billing_agent", tool_name="billing.send_checkout_recovery",
                args={"session_id": a["session_id"], "step": "nudge"})

    _webhook(app_engine, m["mock"], t, ProviderPayment(
        "mock", m["mock"]._next("pay"), Money.of("1500"), PaymentStatus.CAPTURED, "upi", None, None, now,
        order_ref="order_ORIG2002"))
    with tenant_tx(t, app_engine) as c:
        k = c.execute(text("SELECT status, paid_via FROM billing.checkout_sessions WHERE session_id=:s"),
                      {"s": b["session_id"]}).one()
        booked = c.execute(text("SELECT count(*) FROM ledger.entries WHERE memo LIKE '%ORD-2002%'")).scalar_one()
    assert (k.status, k.paid_via, booked) == ("paid", "original", 0)

    # measurement: holdout vs treatment outcomes produce an incremental estimate
    per = {"holdout": [(i < 10, 1000_00 if i < 10 else 0) for i in range(40)],
           "treatment": [(i < 22, 1000_00 if i < 22 else 0) for i in range(60)]}
    inc = checkouts.experiments.incremental(per)["incremental"]["treatment"]
    assert round(inc["incremental_recovery_rate"], 3) == round(22 / 60 - 10 / 40, 3)

    # another business never sees these checkouts
    other = new_id("ten")
    with tenant_tx(other, app_engine) as c:
        create_tenant(c, other, "Someone Else", {"synthetic": True})
    with tenant_tx(other, app_engine) as c:
        assert c.execute(text("SELECT count(*) FROM billing.checkout_sessions")).scalar_one() == 0


def test_diagnosis() -> None:
    d = checkouts.diagnose
    assert d(0, None, "initiated") == "abandoned_cart"
    assert d(0, None, "payment_page") == "abandoned_at_payment"
    assert d(1, "bank_technical_error", "payment_page") == "bank_issue"
    assert d(1, "insufficient_balance", "payment_page") == "insufficient_funds"
    assert d(1, "payment_cancelled", "payment_page") == "customer_cancelled"
    assert d(1, "something_new", "payment_page") == "payment_failed"
    assert d(3, "bank_technical_error", "payment_page") == "repeated_failures"
