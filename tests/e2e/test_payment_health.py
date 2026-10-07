"""Payment degradation → root cause → recovery action (P10, ADR-0024), the brief's first direction, end to end.

Two businesses share an issuer. ~64 hours of normal traffic, then the issuer starts failing (technical declines) and
three of business A's customers are declined through real webhooks. The scan (platform aggregates only) opens an
incident; triage blames the bank, not the customer. The issuer recovers; PaymentHealthWorkflow closes the incident
and sends each affected customer one honest "the bank had a problem" message with a fresh link. One pays — the
proof shows the money; business B sees the incident but none of its customers were hit.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from nirantar.agents.failure_triage import TriageIn, triage
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
from nirantar.health.monitor import degraded_for, scan
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.security.keys import create_principal, issue_api_key
from nirantar.workflows.health import HealthActivities, HealthDeps, HealthInput, PaymentHealthWorkflow
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUEUE = "payment-health-test"


def _midday_zone() -> str:
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _traffic(engine: Engine, tenant: str, issuer: str, at: datetime, n: int, failed: int) -> None:
    """Background payments on the issuer (other customers' everyday traffic)."""
    with tenant_tx(tenant, engine) as c:
        for i in range(n):
            bad = i < failed
            c.execute(text(
                "INSERT INTO billing.payments (tenant_id, payment_id, provider, provider_payment_id, amount_minor, "
                "status, method, issuer, error_code, error_reason, provider_created_at) VALUES (:t, :p, 'mock', :pp, "
                "49900, :s, 'upi', :iss, :ec, :er, :at)"),
                {"t": tenant, "p": new_id("pmt"), "pp": new_id("pay"), "s": "failed" if bad else "captured",
                 "iss": issuer, "ec": "GATEWAY_ERROR" if bad else None, "er": "bank_technical_error" if bad else None,
                 "at": at + timedelta(minutes=i % 60)})


@pytest.fixture()
def world(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    issuer = f"TESTBANK{random.randint(10_000, 99_999)}"           # each run has its own issuer: no shared state
    now = datetime.now(UTC)
    end = _hour(now)
    out: dict[str, Any] = {"issuer": issuer, "now": now, "tenants": {}}
    for name in ("A", "B"):
        t, mock, sink = new_id("ten"), MockProvider(webhook_secret=f"whsec_{name}"), MockCommsSink()
        with tenant_tx(t, app_engine) as c:
            create_tenant(c, t, f"Business {name}", {"segments": ["subscription"], "synthetic": True})
            connect_provider(c, t, "mock", "test", "literal:unused", f"literal:whsec_{name}")
        out["tenants"][name] = {"id": t, "mock": mock, "sink": sink}
        for h in range(70, 6, -1):                                   # normal: ~1-in-30 technical failures
            _traffic(app_engine, t, issuer, end - timedelta(hours=h), 15, 1 if h % 2 else 0)
        for h in range(6, 2, -1):                                    # the incident: 40% technical failures
            _traffic(app_engine, t, issuer, end - timedelta(hours=h), 15, 6)
        for h in (2, 1):                                             # recovered
            _traffic(app_engine, t, issuer, end - timedelta(hours=h), 15, 0)
    # business A: three real customers declined DURING the incident, through the webhook pipeline
    a = out["tenants"]["A"]
    a["customers"], a["subs"] = [], {}
    with tenant_tx(a["id"], app_engine) as c:
        for i in range(3):
            cust = create_customer(c, a["id"], NewCustomer(f"pa{i}", f"Cust {i}", f"+9190007{i:05d}", None, "en",
                                                           consents={"whatsapp": True}))
            c.execute(text("UPDATE billing.customers SET timezone=:z WHERE customer_id=:c"),
                      {"z": _midday_zone(), "c": cust})
            psub = a["mock"].add_subscription(cust, Money.of("499"))
            sub = create_subscription(c, a["id"], cust, "mock", psub, Money.of("499"))
            schedule_debit(c, a["id"], sub, now.date(), now - timedelta(days=2))
            a["customers"].append(cust)
            a["subs"][cust] = psub
    for i, cust in enumerate(a["customers"]):
        p = a["mock"].charge(a["subs"][cust], succeed=False, error_code="GATEWAY_ERROR",
                             error_reason="bank_technical_error", issuer=issuer,
                             at=end - timedelta(hours=5) + timedelta(minutes=10 * i))
        headers, body = a["mock"].webhook_for("payment.failed", "payment", MockProvider.payment_entity(p))
        process_raw_event(app_engine, a["mock"], a["id"],
                          str(ingest_webhook(app_engine, a["mock"], a["id"], headers, body).raw_event_id))
    pid = create_principal(app_engine, a["id"], "owner-a", "user", ["owner"])
    out["key"] = issue_api_key(app_engine, a["id"], pid).plaintext
    yield out


async def test_outage_detected_customers_not_blamed_then_recovered(app_engine: Engine, owner_engine: Engine,
                                                                   world: dict[str, Any]) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    a, b = world["tenants"]["A"], world["tenants"]["B"]
    tenants = [a["id"], b["id"]]
    end = _hour(world["now"])

    # ---- detect: a scan while the issuer was still failing opens one incident with the right start hour
    during = scan(app_engine, owner_engine, tenants, end - timedelta(hours=2, minutes=-5))
    mine = [o for o in during["opened"] if o["issuer"] == world["issuer"]]
    assert len(mine) == 1 and mine[0]["rail"] == "upi"
    assert datetime.fromisoformat(mine[0]["started_at"]) == end - timedelta(hours=6)

    # ---- explain: a failure on this issuer inside the incident is the bank's, not the customer's
    with tenant_tx(a["id"], app_engine) as c:
        failed = c.execute(text("SELECT method, issuer, provider_created_at FROM billing.payments WHERE debit_id IS "
                                "NOT NULL AND status='failed' LIMIT 1")).one()
        incident = degraded_for(c, failed.method, failed.issuer, failed.provider_created_at)
    assert incident == mine[0]["incident_id"]
    verdict = triage(TriageIn(error_code="BAD_REQUEST_ERROR", error_reason="something odd", bank_degraded=True))
    assert verdict.category == "BANK_TECHNICAL" and not verdict.customer_contact_recommended

    # ---- recover: the issuer is healthy again; the workflow closes the incident and messages the affected
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        both_sinks = _FanOutSink({a["id"]: a["sink"], b["id"]: b["sink"]})
        acts = HealthActivities(HealthDeps(app_engine, owner_engine, a["mock"], both_sinks))
        async with Worker(env.client, task_queue=QUEUE, workflows=[PaymentHealthWorkflow], activities=acts.all(),
                          activity_executor=ThreadPoolExecutor(4)):
            handle = await env.client.start_workflow(PaymentHealthWorkflow.run, HealthInput(tenants),
                                                     id=f"health-{a['id']}", task_queue=QUEUE)
            result = await handle.result()
            await capture(handle, "PaymentHealthWorkflow")
    finally:
        await env.shutdown()
    assert result["closed"] >= 1 and result["messages"] == 3
    with owner_engine.connect() as c:
        st = c.execute(text("SELECT status, recovery_done, ended_at FROM core.payment_incidents WHERE incident_id=:i"),
                       {"i": incident}).one()
    assert st.status == "closed" and st.recovery_done and st.ended_at is not None
    msgs = a["sink"].messages
    assert sorted(m.to_ref for m in msgs) == sorted(a["customers"]) and not b["sink"].messages
    assert all("temporary problem at the bank" in m.text and "https://mock.pay/" in m.text for m in msgs)

    # ---- one customer pays the fresh link; the proof is verified money
    link = next(lk for lk in a["mock"].links.values() if lk.url in msgs[0].text)
    paid = a["mock"].pay_link(link.link_id)
    headers, body = a["mock"].webhook_for("payment.captured", "payment", MockProvider.payment_entity(paid))
    process_raw_event(app_engine, a["mock"], a["id"],
                      str(ingest_webhook(app_engine, a["mock"], a["id"], headers, body).raw_event_id))
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
        view = (await api.get("/v1/payment-health", headers={"Authorization": f"Bearer {world['key']}"})).json()
    inc = next(i for i in view["incidents"] if i["incident_id"] == incident)
    assert inc["status"] == "closed" and inc["peak_rate"] >= 0.3 and inc["baseline_rate"] < 0.1
    assert inc["yours"] == {"affected": 3, "contacted": 3, "recovered": 1, "recovered_minor": 49_900}


class _FanOutSink:
    """Routes each send to the business's own mock channel (the deployment has one channel; tests keep two)."""

    def __init__(self, by_tenant: dict[str, MockCommsSink]) -> None:
        self.by_tenant = by_tenant
        self.channels = frozenset({"whatsapp"})

    def send(self, channel: str, to_ref: str, text_: str, at: datetime, *, tenant_id: str | None = None,
             template: Any = None) -> str:
        return self.by_tenant[str(tenant_id)].send(channel, to_ref, text_, at, tenant_id=tenant_id, template=template)
