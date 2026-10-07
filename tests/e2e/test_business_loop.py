"""The real business loop end to end (P8.6, ADR-0022), as a new merchant would run it:

plan → customers (consent with evidence) → enrollment → the first debit is created → the event bridge starts its
DebitCycleWorkflow → on the due date the payment link goes out on WhatsApp in the customer's language →
customer A pays (no webhook: the workflow's 30-minute poll finds it) → verified, ledger settled, closed paid_on_time →
customer B does not pay → NOT_PAID → the recovery agent sends a reminder with a new link → B pays that link →
the poll finds it → closed recovered. Plus the billing clock creating the next cycle, and cancellation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.billing.plans import add_interval, ensure_debits
from nirantar.billing.service import connect_provider, create_tenant
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.mock import MockProvider
from nirantar.security.keys import create_principal, issue_api_key
from nirantar.services.bridge import EventBridge
from nirantar.settings import service as settings
from nirantar.workflows.activities import DebitActivities, Deps
from nirantar.workflows.bridge import workflow_id
from nirantar.workflows.debit_cycle import DebitCycleWorkflow
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUEUE = "business-loop-test"


def _midday_zone() -> str:
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


@pytest.fixture()
def merchant(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    tenant, mock, sink = new_id("ten"), MockProvider(webhook_secret="whsec_bl"), MockCommsSink()
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Chai Club (pilot)", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_bl")
        # these tests exercise collection and recovery, not measurement: no random holdout (8% by default) that
        # would leave a test customer uncontacted by design
        settings.update(c, tenant, "experiments", {"holdout_bp": 0, "holdout_opt_in": False}, actor="user:test",
                        reason="pilot test without holdout", now=datetime.now(UTC), expected_version=0)
    key = issue_api_key(app_engine, tenant, create_principal(app_engine, tenant, "founder", "user", ["owner"])).plaintext
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": mock, "comms": sink})
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine,
                              extra={"billing_gateway": gw, "provider_override": mock}))
    yield {"tenant": tenant, "mock": mock, "sink": sink, "app": app, "h": {"Authorization": f"Bearer {key}"}}


def _scheduled_events(engine: Engine, tenant: str) -> list[EventEnvelope]:
    with tenant_tx(tenant, engine) as c:
        rows = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='subscription.debit_scheduled' "
                              "ORDER BY created_at")).scalars().all()
    return [EventEnvelope.model_validate(r if isinstance(r, dict) else json.loads(r)) for r in rows]


async def test_plan_enroll_collect_verify_recover(app_engine: Engine, merchant: dict[str, Any]) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        # ---- the merchant sets up a plan and adds two customers
        plan = await api.post("/v1/plans", headers=h, json={"name": "Chai Monthly", "amount_rupees": 499,
                                                             "interval": "monthly"})
        assert plan.status_code == 200, plan.text
        pid = plan.json()["plan_id"]
        no_proof = await api.post("/v1/customers", headers=h, json={
            "name": "Ravi Kumar", "phone": "+919000000001", "language": "hi", "whatsapp_consent": True})
        assert no_proof.status_code == 422                                   # consent needs its evidence
        cust = {}
        for who, lang in (("Asha Rao", "hi"), ("Ravi Kumar", "te")):
            r = await api.post("/v1/customers", headers=h, json={
                "name": who, "phone": f"+91900000{len(cust):04d}", "language": lang, "whatsapp_consent": True,
                "consent_source": "signup form at the counter"})
            assert r.status_code == 200, r.text
            cust[who] = r.json()["customer_id"]
        zone = _midday_zone()
        with tenant_tx(t, app_engine) as c:
            c.execute(text("UPDATE billing.customers SET timezone=:z"), {"z": zone})

        # ---- enrollment creates the first debit at once (due today)
        subs = {}
        for who, cid in cust.items():
            r = await api.post("/v1/subscriptions", headers=h, json={"customer_id": cid, "plan_id": pid,
                                                                     "start_on": today.isoformat()})
            assert r.status_code == 200, r.text
            assert len(r.json()["debits_created"]) == 1 and r.json()["collection_method"] == "payment_link"
            subs[who] = r.json()["subscription_id"]
        again = await api.post("/v1/subscriptions", headers=h, json={"customer_id": cust["Asha Rao"],
                                                                     "plan_id": pid, "start_on": today.isoformat()})
        assert again.status_code == 422                                      # one active enrollment per plan

        events = _scheduled_events(app_engine, t)
        assert len(events) == 2
        debits = {ev.payload["customer_id"]: ev.payload["debit_id"] for ev in events}

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            acts = DebitActivities(Deps(app_engine, m["mock"], m["sink"], None, None, None))
            async with Worker(env.client, task_queue=QUEUE, workflows=[DebitCycleWorkflow], activities=acts.all(),
                              activity_executor=ThreadPoolExecutor(8)):
                bridge = EventBridge(app_engine, env.client, m["mock"], task_queue=QUEUE)
                assert [await bridge.handle(ev) for ev in events] == ["started", "started"]
                handles = {who: env.client.get_workflow_handle(workflow_id(t, debits[cid])) for who, cid in cust.items()}

                async def stage(who: str) -> str:
                    return str((await handles[who].query(DebitCycleWorkflow.status))["stage"])

                for _ in range(200):                                         # due date: links go out
                    if all([await stage(w) == "awaiting_payment" for w in handles]):
                        break
                    await env.sleep(timedelta(minutes=10))
                assert all([await stage(w) == "awaiting_payment" for w in handles])
                due_msgs = [msg for msg in m["sink"].messages if "https://mock.pay/" in msg.text]
                assert {msg.to_ref for msg in due_msgs} == set(cust.values())
                assert all("₹499.00" in msg.text and "Chai Monthly" in msg.text for msg in due_msgs)
                assert [tp.key for tp in m["sink"].templates if tp] == ["whatsapp.payment_due"] * 2

                # ---- Asha pays the link; no webhook: the workflow's poll finds the payment
                with tenant_tx(t, app_engine) as c:
                    links = {r.customer_id: r.provider_link_id for r in c.execute(text(
                        "SELECT customer_id, provider_link_id FROM billing.payment_requests"))}
                m["mock"].pay_link(links[cust["Asha Rao"]])
                await env.sleep(timedelta(minutes=35))
                asha = await handles["Asha Rao"].result()
                assert asha["outcome"] == "paid_on_time" and asha["verified"]

                # ---- Ravi does not pay: NOT_PAID → recovery reminder with a fresh link → he pays it
                for _ in range(80):
                    if await stage("Ravi Kumar") in ("awaiting_recovery", "waiting_contact_window"):
                        if any(msg.to_ref == cust["Ravi Kumar"] and msg not in due_msgs for msg in m["sink"].messages):
                            break
                    await env.sleep(timedelta(hours=1))
                reminder = [msg for msg in m["sink"].messages if msg.to_ref == cust["Ravi Kumar"] and msg not in due_msgs]
                assert reminder, "the recovery agent contacted Ravi"
                with tenant_tx(t, app_engine) as c:
                    fresh = c.execute(text("SELECT provider_link_id FROM billing.payment_requests WHERE customer_id=:c "
                                           "ORDER BY created_at DESC LIMIT 1"), {"c": cust["Ravi Kumar"]}).scalar_one()
                    triage = c.execute(text("SELECT count(*) FROM ops.actions WHERE tool_name='comms.send_whatsapp' "
                                            "AND status='executed'")).scalar_one()
                assert fresh != links[cust["Ravi Kumar"]] and triage >= 1
                m["mock"].pay_link(fresh)
                await env.sleep(timedelta(minutes=35))
                ravi = await handles["Ravi Kumar"].result()
                assert ravi["outcome"] == "recovered" and ravi["verified"]
                await capture(handles["Ravi Kumar"], "DebitCycleWorkflow_pay_by_link")
                # post-debit receipts: one per verified payment, in the customer's language, with the payment ref
                receipts = [(tp, msg) for tp, msg in zip(m["sink"].templates, m["sink"].messages, strict=True)
                            if tp and tp.key == "whatsapp.payment_receipt"]
                assert sorted(msg.to_ref for _, msg in receipts) == sorted(cust.values())
                assert {tp.language for tp, _ in receipts} == {"hi", "te"}
                assert all("₹499.00" in msg.text and "pay_" in msg.text for _, msg in receipts)
        finally:
            await env.shutdown()

        # ---- the books: both debits settled through verification, both links marked paid
        with tenant_tx(t, app_engine) as c:
            assert {r[0] for r in c.execute(text("SELECT status FROM billing.debits"))} == {"succeeded"}
            paid = c.execute(text("SELECT count(*) FROM billing.payment_requests WHERE status='paid'")).scalar_one()
            nxt = c.execute(text("SELECT next_charge_on FROM billing.subscriptions WHERE subscription_id=:s"),
                            {"s": subs["Asha Rao"]}).scalar_one()
        assert paid == 2 and nxt == add_interval(today, "monthly")

        # ---- the billing clock: nothing new until the next cycle is inside the horizon
        with tenant_tx(t, app_engine) as c:
            assert ensure_debits(c, t, today, datetime.now(UTC)) == []
            later = add_interval(today, "monthly") - timedelta(days=2)
            created = ensure_debits(c, t, later, datetime.now(UTC))
        assert len(created) == 2

        # ---- cancellation stops future charges
        r = await api.post(f"/v1/subscriptions/{subs['Ravi Kumar']}/cancel", headers=h)
        assert r.status_code == 200 and r.json()["debits_cancelled"] == 1
        listed = (await api.get("/v1/plans", headers=h)).json()["items"]
        assert listed[0]["name"] == "Chai Monthly" and listed[0]["active_subscriptions"] == 1


async def test_manual_payment_link_and_payment_check(app_engine: Engine, merchant: dict[str, Any]) -> None:
    """The founder's 'send link now' button and 'check payments' button, without any workflow running."""
    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        pid = (await api.post("/v1/plans", headers=h, json={"name": "Gym Quarterly", "amount_rupees": 2999,
                                                            "interval": "quarterly"})).json()["plan_id"]
        cid = (await api.post("/v1/customers", headers=h, json={
            "name": "Meera Iyer", "phone": "+919000009999", "language": "en", "whatsapp_consent": True,
            "consent_source": "website checkbox"})).json()["customer_id"]
        with tenant_tx(t, app_engine) as c:
            c.execute(text("UPDATE billing.customers SET timezone=:z"), {"z": _midday_zone()})
        debit = (await api.post("/v1/subscriptions", headers=h, json={
            "customer_id": cid, "plan_id": pid, "start_on": today.isoformat()})).json()["debits_created"][0]
        r = await api.post(f"/v1/debits/{debit}/payment-request", headers=h)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["sent"] and "₹2,999.00" in out["text"] and out["url"].startswith("https://mock.pay/")
        again = (await api.post(f"/v1/debits/{debit}/payment-request", headers=h)).json()
        assert again["url"] == out["url"]                                    # the open link is reused, never doubled
        link_id = next(lk.link_id for lk in m["mock"].links.values() if lk.url == out["url"])
        m["mock"].pay_link(link_id)
        check = (await api.post("/v1/payments/check", headers=h)).json()
        assert check["changed"] >= 1 and "payment.captured" in check["events"]
        with tenant_tx(t, app_engine) as c:
            assert c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"), {"d": debit}).scalar_one() \
                == "succeeded"
        paid_twice = await api.post(f"/v1/debits/{debit}/payment-request", headers=h)
        assert paid_twice.status_code == 409                                 # never ask for money already paid


async def test_billing_clock_runs_as_a_workflow(app_engine: Engine, merchant: dict[str, Any]) -> None:
    """BillingSweepWorkflow on Temporal: a subscription whose next charge enters the horizon gets its debit, once."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from nirantar.workflows.billing import BillingActivities, BillingDeps, BillingSweepInput, BillingSweepWorkflow

    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        pid = (await api.post("/v1/plans", headers=h, json={"name": "Tiffin Weekly", "amount_rupees": 350,
                                                            "interval": "weekly"})).json()["plan_id"]
        cid = (await api.post("/v1/customers", headers=h, json={
            "name": "Kiran Naidu", "phone": "+919000007777", "whatsapp_consent": False})).json()["customer_id"]
        r = (await api.post("/v1/subscriptions", headers=h, json={
            "customer_id": cid, "plan_id": pid, "start_on": (today + timedelta(days=10)).isoformat()})).json()
    assert r["debits_created"] == []                                   # not due yet: nothing to collect
    with tenant_tx(t, app_engine) as c:
        c.execute(text("UPDATE billing.subscriptions SET next_charge_on=:d WHERE subscription_id=:s"),
                  {"d": today + timedelta(days=1), "s": r["subscription_id"]})
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(env.client, task_queue=QUEUE, workflows=[BillingSweepWorkflow],
                          activities=BillingActivities(BillingDeps(app_engine)).all(),
                          activity_executor=ThreadPoolExecutor(2)):
            first = await env.client.execute_workflow(BillingSweepWorkflow.run, BillingSweepInput([t]),
                                                      id=f"billing-{t}-1", task_queue=QUEUE)
            handle = env.client.get_workflow_handle(f"billing-{t}-1")
            await capture(handle, "BillingSweepWorkflow")
            second = await env.client.execute_workflow(BillingSweepWorkflow.run, BillingSweepInput([t]),
                                                       id=f"billing-{t}-2", task_queue=QUEUE)
    finally:
        await env.shutdown()
    assert first == {"tenants": 1, "debits_created": 1} and second["debits_created"] == 0


async def test_hosted_pay_page_confirms_only_verified_payments(app_engine: Engine, merchant: dict[str, Any],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """With a public app URL, the payment request is a Nirantar pay page backed by a provider order. The page shows
    the customer only what they need; confirmation requires the provider signature and re-fetches the payment."""
    monkeypatch.setenv("NIRANTAR_PUBLIC_APP_URL", "https://pay.nirantar.test")
    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        pid = (await api.post("/v1/plans", headers=h, json={"name": "Milk Monthly", "amount_rupees": 1200,
                                                            "interval": "monthly"})).json()["plan_id"]
        cid = (await api.post("/v1/customers", headers=h, json={
            "name": "Lakshmi Devi", "phone": "+919000005555", "language": "te", "whatsapp_consent": True,
            "consent_source": "doorstep signup"})).json()["customer_id"]
        with tenant_tx(t, app_engine) as c:
            c.execute(text("UPDATE billing.customers SET timezone=:z"), {"z": _midday_zone()})
        debit = (await api.post("/v1/subscriptions", headers=h, json={
            "customer_id": cid, "plan_id": pid, "start_on": today.isoformat()})).json()["debits_created"][0]
        out = (await api.post(f"/v1/debits/{debit}/payment-request", headers=h)).json()
        assert out["url"].startswith("https://pay.nirantar.test/pay/ten_") and out["url"] in out["text"]
        token = out["url"].rsplit("/", 1)[1]

        page = (await api.get(f"/v1/public/pay/{token}")).json()            # no credentials: the token is the key
        assert page["business"] == "Chai Club (pilot)" and page["first_name"] == "Lakshmi"
        assert page["amount_minor"] == 120_000 and page["language"] == "te" and page["status"] == "open"
        assert "phone" not in json.dumps(page) and "+91" not in json.dumps(page)
        forged = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
        assert (await api.get(f"/v1/public/pay/{forged}")).status_code == 404

        payment, signature = m["mock"].pay_order(page["order_id"])
        bad = await api.post(f"/v1/public/pay/{token}/confirm", json={
            "razorpay_order_id": page["order_id"], "razorpay_payment_id": payment.provider_payment_id,
            "razorpay_signature": "f" * 64})
        assert bad.status_code == 400                                       # a forged callback never marks it paid
        ok = await api.post(f"/v1/public/pay/{token}/confirm", json={
            "razorpay_order_id": page["order_id"], "razorpay_payment_id": payment.provider_payment_id,
            "razorpay_signature": signature})
        assert ok.status_code == 200 and ok.json()["status"] == "paid"
        again = await api.get(f"/v1/public/pay/{token}")
        assert again.json()["status"] == "paid" and again.json()["order_id"] is None
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"), {"d": debit}).scalar_one() \
            == "succeeded"
        assert c.execute(text("SELECT kind, status FROM billing.payment_requests WHERE debit_id=:d"),
                         {"d": debit}).one() == ("checkout", "paid")


async def test_csv_import_dry_run_then_import_with_enrollment(app_engine: Engine, merchant: dict[str, Any]) -> None:
    """A founder uploads their existing customer list: problems are reported per row, nothing is written on the dry
    run, the real run creates and enrolls the valid rows, and duplicates (in the file and existing) are refused."""
    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date().isoformat()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        await api.post("/v1/plans", headers=h, json={"name": "Chai Monthly", "amount_rupees": 499, "interval": "monthly"})
        csv_text = "\n".join([
            "Name,Phone,Language,WhatsApp_Consent,Consent_Source,Plan,Start_On,Reference",
            f"Asha Rao,98765 43210,hi,yes,counter signup,Chai Monthly,{today},M-1",
            "Ravi Kumar,+91 9876543211,te,no,,,,M-2",
            "Meera Iyer,098765 43212,en,yes,,,,M-3",                       # consent without its evidence
            "Kiran,12345,en,no,,,,M-4",                                    # not a phone number
            "Asha Duplicate,9876543210,hi,no,,,,M-5",                      # same phone as row 2
            f"Vikram Singh,9876543213,xx,no,,Gym Plan,{today},M-6",        # bad language + unknown plan
        ])
        dry = (await api.post("/v1/customers/import", headers=h, json={"csv": csv_text, "dry_run": True})).json()
        assert (dry["rows"], dry["valid"], dry["invalid"]) == (6, 2, 4) and dry["to_enroll"] == 1
        problems = {p["line"]: " ".join(p["problems"]) for p in dry["problems"]}
        assert "consent_source" in problems[4] and "phone" in problems[5] and "duplicate" in problems[6]
        assert "language" in problems[7] and "Gym Plan" in problems[7]
        with tenant_tx(t, app_engine) as c:
            assert c.execute(text("SELECT count(*) FROM billing.customers")).scalar_one() == 0   # dry run wrote nothing
        real = (await api.post("/v1/customers/import", headers=h, json={"csv": csv_text, "dry_run": False})).json()
        assert real["created"] == 2 and real["enrolled"] == 1 and real["debits_created"] == 1
        again = (await api.post("/v1/customers/import", headers=h, json={"csv": csv_text, "dry_run": True})).json()
        assert again["valid"] == 0                                        # existing phones are now duplicates
    with tenant_tx(t, app_engine) as c:
        rows = c.execute(text("SELECT display_name, consents, preferred_language FROM billing.customers "
                              "ORDER BY display_name")).all()
    assert [r.display_name for r in rows] == ["Asha Rao", "Ravi Kumar"]
    asha = rows[0].consents if isinstance(rows[0].consents, dict) else json.loads(rows[0].consents)
    assert asha["whatsapp"] is True and asha["evidence"]["whatsapp"]["via"] == "csv_import"


async def test_pay_page_payment_settles_from_the_webhook_alone(app_engine: Engine, merchant: dict[str, Any],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Customer pays on the pay page and closes the tab before our confirm call: Razorpay's payment.captured webhook
    (which carries the order id, not our notes) is enough to settle the right debit."""
    from nirantar.payments.ingress import ingest_webhook
    from nirantar.payments.processing import process_raw_event
    from nirantar.payments.providers.mock import MockProvider

    monkeypatch.setenv("NIRANTAR_PUBLIC_APP_URL", "https://pay.nirantar.test")
    m, t, h = merchant, merchant["tenant"], merchant["h"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        pid = (await api.post("/v1/plans", headers=h, json={"name": "Coaching", "amount_rupees": 1500,
                                                            "interval": "monthly"})).json()["plan_id"]
        cid = (await api.post("/v1/customers", headers=h, json={
            "name": "Arjun Rao", "phone": "+919000004444", "whatsapp_consent": True,
            "consent_source": "admission form"})).json()["customer_id"]
        with tenant_tx(t, app_engine) as c:
            c.execute(text("UPDATE billing.customers SET timezone=:z"), {"z": _midday_zone()})
        debit = (await api.post("/v1/subscriptions", headers=h, json={
            "customer_id": cid, "plan_id": pid, "start_on": datetime.now(UTC).date().isoformat()})
        ).json()["debits_created"][0]
        token = (await api.post(f"/v1/debits/{debit}/payment-request", headers=h)).json()["url"].rsplit("/", 1)[1]
        order = (await api.get(f"/v1/public/pay/{token}")).json()["order_id"]
    payment, _ = m["mock"].pay_order(order)
    assert "nirantar_ref" not in payment.notes and payment.order_ref == order
    headers, body = m["mock"].webhook_for("payment.captured", "payment", MockProvider.payment_entity(payment))
    res = ingest_webhook(app_engine, m["mock"], t, headers, body)
    process_raw_event(app_engine, m["mock"], t, str(res.raw_event_id))
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"), {"d": debit}).scalar_one() \
            == "succeeded"


async def test_promise_to_pay_pauses_chasing_reminds_and_resolves(app_engine: Engine, merchant: dict[str, Any]) -> None:
    """Both customers miss the due date and get a recovery message. Asha replies "parso pay karunga": nothing more is
    sent until that day, then a reminder with a link; she pays → promise kept. Ravi replies "kal pay kar dunga", gets
    his reminder and does not pay → promise broken, and recovery resumes with a fresh contact."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    m, t, h = merchant, merchant["tenant"], merchant["h"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        pid = (await api.post("/v1/plans", headers=h, json={"name": "Tiffin Monthly", "amount_rupees": 2400,
                                                            "interval": "monthly"})).json()["plan_id"]
        cust = {}
        for i, who in enumerate(("Asha Rao", "Ravi Kumar")):
            cust[who] = (await api.post("/v1/customers", headers=h, json={
                "name": who, "phone": f"+9190000020{i:02d}", "language": "hi", "whatsapp_consent": True,
                "consent_source": "signup form"})).json()["customer_id"]
        with tenant_tx(t, app_engine) as c:
            c.execute(text("UPDATE billing.customers SET timezone='Asia/Kolkata'"))
        for cid in cust.values():
            await api.post("/v1/subscriptions", headers=h, json={"customer_id": cid, "plan_id": pid,
                                                                 "start_on": today.isoformat()})
    events = _scheduled_events(app_engine, t)
    debits = {ev.payload["customer_id"]: ev.payload["debit_id"] for ev in events}

    def sent_to(cid: str) -> list[Any]:
        return [(tp, msg) for tp, msg in zip(m["sink"].templates, m["sink"].messages, strict=True) if msg.to_ref == cid]

    env = await WorkflowEnvironment.start_time_skipping()
    try:
        acts = DebitActivities(Deps(app_engine, m["mock"], m["sink"], None, None, None))
        async with Worker(env.client, task_queue=QUEUE, workflows=[DebitCycleWorkflow], activities=acts.all(),
                          activity_executor=ThreadPoolExecutor(8)):
            bridge = EventBridge(app_engine, env.client, m["mock"], task_queue=QUEUE)
            for ev in events:
                await bridge.handle(ev)
            handles = {w: env.client.get_workflow_handle(workflow_id(t, debits[c])) for w, c in cust.items()}

            async def stage(w: str) -> str:
                return str((await handles[w].query(DebitCycleWorkflow.status))["stage"])

            # ---- both reach recovery and get the first recovery message
            for _ in range(120):
                if all([await stage(w) == "awaiting_recovery" for w in handles]):
                    break
                await env.sleep(timedelta(hours=1))
            assert all([await stage(w) == "awaiting_recovery" for w in handles])
            for w in handles:
                await handles[w].signal(DebitCycleWorkflow.customer_reply, {
                    "message_id": new_id("msg"), "text": "parso pay karunga" if w == "Asha Rao" else "kal pay kar dunga"})
            now_day = (await env.get_current_time()).astimezone(UTC).date()
            await env.sleep(timedelta(minutes=5))
            with tenant_tx(t, app_engine) as c:
                promises = {r.customer_id: r for r in c.execute(text(
                    "SELECT customer_id, promised_date, status, quote FROM ops.promises"))}
            assert promises[cust["Asha Rao"]].promised_date == now_day + timedelta(days=2)
            assert promises[cust["Ravi Kumar"]].promised_date == now_day + timedelta(days=1)
            assert promises[cust["Asha Rao"]].quote == "parso pay karunga"
            assert all([await stage(w) == "promised" for w in handles])
            before = {w: len(sent_to(c)) for w, c in cust.items()}

            # ---- quiet until the promised day (only the acknowledgement of their reply was sent)
            await env.sleep(timedelta(hours=18))
            ack_only = {w: [tp.key if tp else "free text" for tp, _ in sent_to(c)[before[w]:]] for w, c in cust.items()}
            assert all("whatsapp.promise_reminder" not in keys for keys in ack_only.values())

            # ---- Ravi's day (tomorrow): reminder; he does not pay → broken → recovery resumes
            for _ in range(60):
                if any(tp and tp.key == "whatsapp.promise_reminder" for tp, _ in sent_to(cust["Ravi Kumar"])):
                    break
                await env.sleep(timedelta(hours=1))
            assert any(tp and tp.key == "whatsapp.promise_reminder" for tp, _ in sent_to(cust["Ravi Kumar"]))
            assert not any(tp and tp.key == "whatsapp.promise_reminder" for tp, _ in sent_to(cust["Asha Rao"]))

            # ---- Asha's day: reminder, then she pays
            for _ in range(60):
                if any(tp and tp.key == "whatsapp.promise_reminder" for tp, _ in sent_to(cust["Asha Rao"])):
                    break
                await env.sleep(timedelta(hours=1))
            with tenant_tx(t, app_engine) as c:
                link = c.execute(text("SELECT provider_link_id FROM billing.payment_requests WHERE customer_id=:c "
                                      "ORDER BY created_at DESC LIMIT 1"), {"c": cust["Asha Rao"]}).scalar_one()
            m["mock"].pay_link(link)
            for _ in range(200):                                  # the 30-minute poll finds the payment
                if await stage("Asha Rao") == "closed":
                    break
                await env.sleep(timedelta(minutes=30))
            asha = await handles["Asha Rao"].result()
            assert asha["outcome"] == "recovered" and asha["verified"]

            for _ in range(60):
                with tenant_tx(t, app_engine) as c:
                    st = c.execute(text("SELECT status FROM ops.promises WHERE customer_id=:c"),
                                   {"c": cust["Ravi Kumar"]}).scalar_one()
                if st == "broken":
                    break
                await env.sleep(timedelta(hours=1))
            assert st == "broken"
            # recovery resumes after the broken promise — and stays compliant: Ravi has already had 4 messages this
            # week (due date, recovery, acknowledgement, reminder), so the next contact is refused by the fatigue budget
            # rather than sent. The round is recorded with its policy decision.
            reminder_time = max(msg.at for tp, msg in sent_to(cust["Ravi Kumar"])
                                if tp and tp.key == "whatsapp.promise_reminder")
            for _ in range(96):
                with tenant_tx(t, app_engine) as c:
                    after = c.execute(text(
                        "SELECT tool_name, status, policy_decision FROM ops.actions WHERE created_at > :r AND "
                        "case_id = :k AND agent_id='conductor' AND tool_name='policy.check_action'"),
                        {"r": reminder_time, "k": "cas_" + debits[cust["Ravi Kumar"]].split("_", 1)[1]}).all()
                if after:
                    break
                await env.sleep(timedelta(hours=1))
            assert after, "a recovery round ran after the broken promise"
            assert len(sent_to(cust["Ravi Kumar"])) == 4                   # …and the fatigue budget held
            await capture(handles["Asha Rao"], "DebitCycleWorkflow_promise_kept")
    finally:
        await env.shutdown()
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status FROM ops.promises WHERE customer_id=:c"),
                         {"c": cust["Asha Rao"]}).scalar_one() == "kept"
