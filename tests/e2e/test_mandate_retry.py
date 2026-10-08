"""UPI AutoPay mandate collection and the retry sequencer end to end (ADR-0029), on real Postgres + Temporal.

A customer with an active mandate is enrolled on a mandate plan → pre-debit notice → Nirantar charges the mandate on
the due date → the bank fails (technical) → the sequencer plans a retry, notifies the customer ≥24h ahead, charges in
the morning window → fails again (insufficient funds) → plans the last allowed retry → succeeds → verified, booked once,
closed recovered. Then: a revoked mandate is never retried, and the charge tool refuses unsafe charges.
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
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.mandates.retry import MAX_ATTEMPTS
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
QUEUE = "mandate-retry-test"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def merchant(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    tenant, mock, sink = new_id("ten"), MockProvider(webhook_secret="whsec_md"), MockCommsSink()
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Dhoodh Daily", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_md")
        settings.update(c, tenant, "experiments", {"holdout_bp": 0, "holdout_opt_in": False}, actor="user:test",
                        reason="collection under test", now=datetime.now(UTC), expected_version=0)
    key = issue_api_key(app_engine, tenant, create_principal(app_engine, tenant, "founder", "user", ["owner"])).plaintext
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine, extra={"provider_override": mock}))
    yield {"tenant": tenant, "mock": mock, "sink": sink, "app": app, "h": {"Authorization": f"Bearer {key}"}}


def _mandate(engine: Engine, tenant: str, mock: MockProvider, customer_id: str, limit: str = "2000") -> str:
    token = mock.add_mandate(f"cust_{customer_id[-6:]}", max_amount=Money.of(limit))
    with tenant_tx(tenant, engine) as c:
        c.execute(text("INSERT INTO billing.mandates (tenant_id, mandate_id, customer_id, provider, provider_token_id, "
                       "rail, max_amount_minor, status, provider_customer_ref) VALUES (:t, :m, :c, 'mock', :tok, "
                       "'upi_autopay', :lim, 'active', :cr)"),
                  {"t": tenant, "m": new_id("mdt"), "c": customer_id, "tok": token,
                   "lim": Money.of(limit).minor, "cr": f"cust_{customer_id[-6:]}"})
    return token


async def _setup(api: httpx.AsyncClient, m: dict[str, Any], engine: Engine) -> tuple[str, str]:
    plan = await api.post("/v1/plans", headers=m["h"], json={"name": "Milk Monthly", "amount_rupees": 1200,
                                                             "interval": "monthly", "collection_method": "mandate"})
    assert plan.status_code == 200, plan.text
    r = await api.post("/v1/customers", headers=m["h"], json={
        "name": "Kavya Nair", "phone": "+919000007777", "language": "en", "whatsapp_consent": True,
        "consent_source": "app signup"})
    cid = r.json()["customer_id"]
    start = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    # no mandate yet: enrolment on a mandate plan is refused with the way forward
    refused = await api.post("/v1/subscriptions", headers=m["h"], json={"customer_id": cid,
                                                                       "plan_id": plan.json()["plan_id"],
                                                                       "start_on": start})
    assert refused.status_code == 422 and "mandate" in refused.text
    _mandate(engine, m["tenant"], m["mock"], cid)
    r = await api.post("/v1/subscriptions", headers=m["h"], json={"customer_id": cid,
                                                                 "plan_id": plan.json()["plan_id"], "start_on": start})
    assert r.status_code == 200 and r.json()["collection_method"] == "mandate", r.text
    return cid, r.json()["debits_created"][0]


def _events(engine: Engine, tenant: str) -> list[EventEnvelope]:
    with tenant_tx(tenant, engine) as c:
        rows = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='subscription.debit_scheduled' "
                              "ORDER BY created_at")).scalars().all()
    return [EventEnvelope.model_validate(r if isinstance(r, dict) else json.loads(r)) for r in rows]


async def _run(m: dict[str, Any], engine: Engine, debit: str, until: Any) -> Any:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    env = await WorkflowEnvironment.start_time_skipping()
    try:
        acts = DebitActivities(Deps(engine, m["mock"], m["sink"], None, None, None))
        async with Worker(env.client, task_queue=QUEUE, workflows=[DebitCycleWorkflow], activities=acts.all(),
                          activity_executor=ThreadPoolExecutor(8)):
            bridge = EventBridge(engine, env.client, m["mock"], task_queue=QUEUE)
            assert [await bridge.handle(ev) for ev in _events(engine, m["tenant"])] == ["started"]
            h = env.client.get_workflow_handle(workflow_id(m["tenant"], debit))
            for _ in range(400):
                if (await h.describe()).status.name != "RUNNING" or until():
                    break
                await env.sleep(timedelta(hours=3))
            result = await h.result() if (await h.describe()).status.name != "RUNNING" else None
            if result is not None and until is _never:
                await capture(h, "DebitCycleWorkflow_mandate_retry")
            return result
    finally:
        await env.shutdown()


def _never() -> bool:
    return False


async def test_bank_failure_then_low_balance_then_paid_within_the_cap(app_engine: Engine,
                                                                      merchant: dict[str, Any]) -> None:
    m, t = merchant, merchant["tenant"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        _, debit = await _setup(api, m, app_engine)
    m["mock"].mandate_outcomes = [(False, "bank_technical_error"), (False, "insufficient_balance"), (True, None)]
    result = await _run(m, app_engine, debit, _never)
    assert result is not None and result["outcome"] == "recovered" and result["verified"] is True

    charges = m["mock"].mandate_charges
    assert [c["receipt"] for c in charges] == [f"{debit}.a1", f"{debit}.a2", f"{debit}.a3"]   # never beyond the cap
    assert all(c["amount_minor"] == 120_000 for c in charges)                           # always the debit's amount
    with tenant_tx(t, app_engine) as c:
        attempts = c.execute(text("SELECT attempt, kind, status, failure_category, notice_sent_at, charged_at FROM "
                                  "billing.debit_attempts WHERE debit_id=:d ORDER BY attempt"), {"d": debit}).all()
        settled = c.execute(text("SELECT count(*) FROM ledger.entries WHERE idempotency_key LIKE 'settle:%' AND "
                                 "memo LIKE :d"), {"d": f"%{debit}%"}).scalar_one()
    assert [(a.attempt, a.kind, a.status) for a in attempts] == [(1, "initial", "failed"), (2, "retry", "failed"),
                                                                 (3, "retry", "captured")]
    assert [a.failure_category for a in attempts[1:]] == ["BANK_TECHNICAL", "INSUFFICIENT_FUNDS"]
    for a in attempts:                                   # RBI: every charge ≥24h after its pre-debit notice
        assert a.notice_sent_at is not None and a.charged_at - a.notice_sent_at >= timedelta(hours=24)
    for a in attempts[1:]:                               # retries only in the early-morning window
        assert 6 <= a.charged_at.astimezone(IST).hour < 9
    assert settled == 1
    assert len(attempts) <= MAX_ATTEMPTS


async def test_revoked_mandate_is_never_retried(app_engine: Engine, merchant: dict[str, Any]) -> None:
    m, t = merchant, merchant["tenant"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
        _, debit = await _setup(api, m, app_engine)
    m["mock"].mandate_outcomes = [(False, "mandate_revoked")]

    def contacted() -> bool:
        with tenant_tx(t, app_engine) as c:
            return bool(c.execute(text("SELECT count(*) FROM ops.actions WHERE tool_name LIKE 'comms.%' AND "
                                       "tool_name <> 'comms.send_predebit_notice' AND status='executed'")).scalar_one())

    await _run(m, app_engine, debit, contacted)
    assert len(m["mock"].mandate_charges) == 1                                     # one charge, no blind retries
    with tenant_tx(t, app_engine) as c:
        n = c.execute(text("SELECT count(*) FROM billing.debit_attempts WHERE debit_id=:d"), {"d": debit}).scalar_one()
    assert n == 1


def test_the_charge_tool_refuses_unsafe_charges(app_engine: Engine, merchant: dict[str, Any]) -> None:
    import asyncio

    m, t = merchant, merchant["tenant"]

    async def setup() -> tuple[str, str]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m["app"]), base_url="http://t") as api:
            return await _setup(api, m, app_engine)

    _, debit = asyncio.run(setup())
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": m["mock"], "comms": m["sink"]})

    def charge(attempt: int) -> Any:
        return gw.call(tenant_id=t, agent_id="retry_sequencer", tool_name="mandate.charge_debit",
                       args={"debit_id": debit, "attempt": attempt})

    first = charge(1)                     # no pre-debit notice has gone out yet: refused
    assert first.status == "failed" and "24 hours" in (first.error or "")
    assert charge(2).status == "failed"   # no planned retry
    assert charge(MAX_ATTEMPTS + 1).status == "failed"
    assert m["mock"].mandate_charges == []
    with pytest.raises(Exception, match="not allowed"):              # only the sequencer can charge
        gw.call(tenant_id=t, agent_id="billing_agent", tool_name="mandate.charge_debit",
                args={"debit_id": debit, "attempt": 1})
