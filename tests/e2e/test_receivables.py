"""B2B receivables chaser end to end (P10, ADR-0025) — the brief's "B2B receivables chaser" with its bar:
compliant escalation (the final notice waits for a person), stopping rules (paid / disputed stop the ladder at once),
measured money (partial payments verified and ledgered) and an audit trail (every step recorded with its decision).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.billing.service import NewCustomer, connect_provider, create_customer, create_tenant
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope
from nirantar.core.clock import FixedClock
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
from nirantar.workflows.receivables import InvoiceChaseWorkflow, invoice_workflow_id
from nirantar.workflows.receivables_activities import ReceivablesActivities, ReceivablesDeps
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUEUE = "receivables-test"


def _midday_zone() -> str:
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


@pytest.fixture()
def b2b(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    t, mock, sink = new_id("ten"), MockProvider(webhook_secret="whsec_b2b"), MockCommsSink()
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Sharma Distributors", {"segments": ["b2b"], "synthetic": True})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_b2b")
        cust = create_customer(c, t, NewCustomer("RET-7", "Lakshmi Stores", "+919000003333", None, "en", "b2b",
                                                 consents={"whatsapp": True}))
        c.execute(text("UPDATE billing.customers SET timezone='Asia/Kolkata'"))
    owner = issue_api_key(app_engine, t, create_principal(app_engine, t, "owner", "user", ["owner"])).plaintext
    analyst = issue_api_key(app_engine, t, create_principal(app_engine, t, "ops", "user", ["ops_analyst"])).plaintext
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": mock, "comms": sink})
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine,
                              extra={"receivables_gateway": gw, "provider_override": mock}))
    yield {"tenant": t, "mock": mock, "sink": sink, "customer": cust, "app": app,
           "owner": {"Authorization": f"Bearer {owner}"}, "analyst": {"Authorization": f"Bearer {analyst}"}}


def _pay(engine: Engine, mock: MockProvider, tenant: str, invoice_id: str, rupees: str) -> None:
    """The customer pays `rupees` against the invoice (a partial payment is normal in B2B)."""
    p = ProviderPayment("mock", mock._next("pay"), Money.of(rupees), PaymentStatus.CAPTURED, "upi", None, None,
                        datetime.now(UTC), notes={"reference_id": f"{invoice_id}.manual"})
    mock.payments[p.provider_payment_id] = p
    headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(p))
    process_raw_event(engine, mock, tenant, str(ingest_webhook(engine, mock, tenant, headers, body).raw_event_id))


async def test_invoice_ladder_partial_payments_approval_dispute_and_ageing(app_engine: Engine,
                                                                          b2b: dict[str, Any]) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    m, t = b2b, b2b["tenant"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        ids = {}
        for number, rupees, due_in in (("INV-X", 10_000, 3), ("INV-Y", 25_000, 3), ("INV-Z", 4_000, 5)):
            r = await api.post("/v1/invoices", headers=m["owner"], json={
                "customer_id": m["customer"], "number": number, "amount_rupees": rupees,
                "issued_on": today.isoformat(), "due_on": (today + timedelta(days=due_in)).isoformat()})
            assert r.status_code == 200, r.text
            ids[number] = r.json()["invoice_id"]
        dup = await api.post("/v1/invoices", headers=m["owner"], json={
            "customer_id": m["customer"], "number": "INV-X", "amount_rupees": 1, "issued_on": today.isoformat(),
            "due_on": today.isoformat()})
        assert dup.status_code == 422                                          # invoice numbers are unique
        with tenant_tx(t, app_engine) as c:
            accrued = c.execute(text("SELECT count(*) FROM ledger.entries WHERE memo LIKE 'invoice issued%'")).scalar_one()
            events = [EventEnvelope.model_validate(r if isinstance(r, dict) else json.loads(r)) for r in c.execute(text(
                "SELECT envelope FROM events.outbox WHERE event_type='invoice.issued' ORDER BY created_at")).scalars()]
        assert accrued == 3 and len(events) == 3                               # booked as receivable at issue

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            acts = ReceivablesActivities(ReceivablesDeps(app_engine, m["mock"], m["sink"]))
            async with Worker(env.client, task_queue=QUEUE, workflows=[InvoiceChaseWorkflow], activities=acts.all(),
                              activity_executor=ThreadPoolExecutor(4)):
                bridge = EventBridge(app_engine, env.client, m["mock"], task_queue=QUEUE)
                assert [await bridge.handle(ev) for ev in events] == ["started"] * 3
                h = {n: env.client.get_workflow_handle(invoice_workflow_id(t, i)) for n, i in ids.items()}

                def steps(inv: str) -> dict[str, str]:
                    with tenant_tx(t, app_engine) as c:
                        return dict(c.execute(text("SELECT step, status FROM ops.invoice_chases WHERE invoice_id=:i"),
                                              {"i": ids[inv]}).all())

                async def finished(name: str) -> dict[str, Any]:
                    from temporalio.client import WorkflowExecutionStatus

                    for _ in range(200):                     # the ladder notices at its next 6-hourly check
                        if (await h[name].describe()).status != WorkflowExecutionStatus.RUNNING:
                            break
                        await env.sleep(timedelta(hours=6))
                    out: dict[str, Any] = await h[name].result()
                    return out

                async def advance_until(cond: Any, hours: int = 900) -> None:
                    for _ in range(hours // 6):
                        if cond():
                            return
                        await env.sleep(timedelta(hours=6))
                    assert cond()

                # ---- reminders before the due date (3 days before = today for X and Y): ONE statement for the
                # customer's three open invoices, not three messages
                # (whatever the wall-clock time the test runs at: a step denied by the contact window is deferred)
                sent = ("executed", "bundled")
                await advance_until(lambda: steps("INV-X").get("reminder") in sent
                                    and steps("INV-Y").get("reminder") in sent)
                first = [msg for tp, msg in zip(m["sink"].templates, m["sink"].messages, strict=True)
                         if tp and tp.key == "whatsapp.invoice_statement"]
                assert len(first) == 1 and all(n in first[0].text for n in ("INV-X", "INV-Y", "INV-Z"))
                assert "₹39,000.00" in first[0].text                         # 10,000 + 25,000 + 4,000
                # ---- Z is disputed: the ladder stops at its next check, without another message
                r = await api.post(f"/v1/invoices/{ids['INV-Z']}/dispute", headers=m["owner"],
                                   json={"reason": "two cartons arrived damaged"})
                assert r.status_code == 200
                # ---- X pays ₹4,000 now (partial), the rest after the first overdue follow-up
                _pay(app_engine, m["mock"], t, ids["INV-X"], "4000")
                await advance_until(lambda: "overdue_1" in steps("INV-X"))
                with tenant_tx(t, app_engine) as c:
                    latest = c.execute(text("SELECT amount_minor, invoice_ids FROM billing.payment_requests WHERE "
                                            ":i = ANY(invoice_ids) ORDER BY created_at DESC LIMIT 1"),
                                       {"i": ids["INV-X"]}).one()
                # follow-ups ask only for what is owed: X's remaining ₹6,000 + Y's ₹25,000; disputed Z is left out
                assert latest.amount_minor == 600_000 + 2_500_000 and ids["INV-Z"] not in latest.invoice_ids
                _pay(app_engine, m["mock"], t, ids["INV-X"], "6000")
                x = await finished("INV-X")
                assert x["outcome"] == "paid"
                z = await finished("INV-Z")
                assert z["outcome"] == "disputed"
                # ---- Y never pays: overdue 1 & 2, the final notice waits for a person, then a collections case
                await advance_until(lambda: "human" in steps("INV-Y"))
                y = await finished("INV-Y")
                await capture(h["INV-Y"], "InvoiceChaseWorkflow")
        finally:
            await env.shutdown()
        assert y["outcome"] == "escalated"
        y_steps = steps("INV-Y")
        assert set(y_steps) == {"reminder", "due", "overdue_1", "overdue_2", "final", "human"}
        assert y_steps["final"] == "pending_approval" and y_steps["human"] == "executed"
        # the same customer got reminders for three invoices this week: the per-customer contact budget refuses some
        # follow-ups — compliance over persistence — and the ladder carries on with the next step
        with tenant_tx(t, app_engine) as c:
            refused = {r.step: r.detail for r in c.execute(text(
                "SELECT step, detail FROM ops.invoice_chases WHERE invoice_id=:i AND status='denied'"),
                {"i": ids["INV-Y"]})}
        assert all(any("contact budget" in msg for msg in d["messages"]) for d in refused.values())
        assert sum(st == "executed" for st in y_steps.values()) >= 3
        assert "final" not in steps("INV-X") and set(steps("INV-Z")) <= {"reminder"}
        with tenant_tx(t, app_engine) as c:
            case = c.execute(text("SELECT kind, status FROM ops.cases WHERE subject_id=:i"), {"i": ids["INV-Y"]}).one()
            approvals = c.execute(text("SELECT count(*) FROM ops.approvals WHERE status='pending'")).scalar_one()
            settled = c.execute(text("SELECT coalesce(sum(l.amount_minor), 0) FROM ledger.lines l JOIN ledger.entries e "
                                     "ON e.tenant_id=l.tenant_id AND e.entry_id=l.entry_id WHERE e.memo LIKE "
                                     "'verified payment for invoice INV-X%' AND l.side='C'")).scalar_one()
        assert tuple(case) == ("collections", "escalated") and approvals == 1 and settled == 1_000_000
        texts = [msg.text for msg in m["sink"].messages]
        assert not any("INV-Z" in tx for tx in texts[1:] if "INV-Z" in tx and texts.index(tx) > 2)
        assert all("₹" in tx for tx in texts)

        # ---- the views: ageing and permissions
        view = (await api.get("/v1/invoices", headers=m["owner"])).json()
        by = {i["number"]: i for i in view["items"]}
        assert by["INV-X"]["status"] == "paid" and by["INV-X"]["paid_minor"] == 1_000_000
        assert by["INV-Y"]["status"] == "open" and by["INV-Z"]["status"] == "disputed"
        assert view["ageing"]["outstanding_minor"] == 2_500_000                # Y only; Z is disputed, X paid
        denied = await api.post(f"/v1/invoices/{ids['INV-Y']}/write-off", headers=m["analyst"], json={"reason": "x"})
        assert denied.status_code == 403                                       # write-off is a finance decision
        ok = await api.post(f"/v1/invoices/{ids['INV-Y']}/write-off", headers=m["owner"], json={"reason": "store closed"})
        assert ok.status_code == 200


async def test_statement_pay_all_settles_every_invoice_oldest_first(app_engine: Engine, b2b: dict[str, Any]) -> None:
    """One 'pay all' payment against a statement: verified once, booked once, split oldest-due-first across the
    invoices, every split recorded — and a smaller payment pays the oldest invoice first."""
    m, t = b2b, b2b["tenant"]
    today = datetime.now(UTC).date()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        ids = {}
        for number, rupees, due_in in (("ST-OLD", 3_000, 1), ("ST-NEW", 2_000, 10), ("ST-MID", 1_500, 5)):
            ids[number] = (await api.post("/v1/invoices", headers=m["owner"], json={
                "customer_id": m["customer"], "number": number, "amount_rupees": rupees,
                "issued_on": today.isoformat(), "due_on": (today + timedelta(days=due_in)).isoformat()})).json()["invoice_id"]
    at_10_ist = datetime.combine(today, time(4, 30), tzinfo=UTC)                 # inside the contact window
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": m["mock"], "comms": m["sink"]},
                     clock=FixedClock(at_10_ist))
    r = gw.call(tenant_id=t, agent_id="receivables_agent", tool_name="billing.send_invoice_statement",
                args={"customer_id": m["customer"]})
    assert r.status == "executed" and r.output["owed_minor"] == 650_000
    assert "ST-OLD ₹3,000.00" in r.output["text"] and r.output["text"].index("ST-OLD") < r.output["text"].index("ST-NEW")
    # the customer pays ₹4,000 of ₹6,500 through the statement link: ST-OLD fully, ST-MID ₹1,000, ST-NEW untouched
    link_id = next(lk.link_id for lk in m["mock"].links.values() if lk.url == r.output["url"])
    p = ProviderPayment("mock", m["mock"]._next("pay"), Money.of("4000"), PaymentStatus.CAPTURED, "upi", None, None,
                        datetime.now(UTC), notes={"reference_id": r.output["request_id"]})
    m["mock"].payments[p.provider_payment_id] = p
    m["mock"].link_payments.setdefault(link_id, []).append(p.provider_payment_id)
    headers, body = m["mock"].webhook_for("payment.captured", "payment", MockProvider.payment_entity(p))
    process_raw_event(app_engine, m["mock"], t, str(ingest_webhook(app_engine, m["mock"], t, headers, body).raw_event_id))
    with tenant_tx(t, app_engine) as c:
        state = {r.number: (r.status, int(r.paid_minor)) for r in c.execute(text(
            "SELECT number, status, paid_minor FROM billing.invoices WHERE invoice_id = ANY(:i)"),
            {"i": list(ids.values())})}
        splits = c.execute(text("SELECT count(*), sum(amount_minor) FROM billing.payment_allocations WHERE "
                                "provider_payment_id=:p"), {"p": p.provider_payment_id}).one()
        entries = c.execute(text("SELECT count(*) FROM ledger.entries WHERE memo LIKE 'verified payment for invoice%' "
                                 "AND memo LIKE '%ST-OLD%'")).scalar_one()
    assert state == {"ST-OLD": ("paid", 300_000), "ST-MID": ("partially_paid", 100_000), "ST-NEW": ("open", 0)}
    assert tuple(splits) == (2, 400_000) and entries == 1                  # verified once, booked once, split twice
    # replaying the same webhook changes nothing
    process_raw_event(app_engine, m["mock"], t, str(ingest_webhook(app_engine, m["mock"], t, headers, body).raw_event_id))
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT count(*) FROM billing.payment_allocations WHERE provider_payment_id=:p"),
                         {"p": p.provider_payment_id}).scalar_one() == 2
