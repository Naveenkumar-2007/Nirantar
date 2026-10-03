"""A2A v1.0 server through the OFFICIAL a2a-sdk client over real HTTP (P7, ADR-0016): agent card discovery, partner
bearer auth, both skills through the ToolGateway, merchant approval reflected in GetTask, input-required, isolation."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.comms.sink import MockCommsSink
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.demo.seed import seed
from nirantar.payments.providers.mock import MockProvider, _Sub

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def stack(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    import uvicorn

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    old = os.environ.get("NIRANTAR_PUBLIC_URL")
    os.environ["NIRANTAR_PUBLIC_URL"] = base                    # the card advertises the real endpoint
    demo = seed(app_engine, n_customers=10, seed_value=11)
    mock = MockProvider()
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine, provider=mock, comms=MockCommsSink()))
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=uv.run, daemon=True)
    th.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    owner = {"Authorization": f"Bearer {demo['api_keys']['owner']}"}
    api = TestClient(app)
    partner = api.post("/v1/a2a/partners", json={"name": "Priya's assistant"}, headers=owner).json()
    other = api.post("/v1/a2a/partners", json={"name": "Another agent"}, headers=owner).json()
    with tenant_tx(demo["tenant_id"], app_engine) as c:
        cust = c.execute(text("SELECT c.external_ref, c.customer_id FROM billing.customers c JOIN billing.subscriptions "
                              "s ON s.customer_id=c.customer_id AND s.tenant_id=c.tenant_id WHERE s.status='active' "
                              "LIMIT 1")).one()
        subs = c.execute(text("SELECT provider_subscription_id, amount_minor FROM billing.subscriptions WHERE "
                              "customer_id=:c"), {"c": cust.customer_id}).all()
    # the API's provider is a fresh mock: register the customer's provider-side subscriptions so the pause can act
    for psub, amount in subs:
        mock.subs[psub] = _Sub(psub, cust.customer_id, Money(int(amount)))
    yield {"base": base, "demo": demo, "api": api, "owner": owner, "partner": partner, "other": other,
           "customer_ref": cust.external_ref, "customer_id": cust.customer_id, "mock": mock}
    uv.should_exit = True
    th.join(5)
    if old is None:
        os.environ.pop("NIRANTAR_PUBLIC_URL", None)
    else:
        os.environ["NIRANTAR_PUBLIC_URL"] = old


async def _client(st: dict[str, Any], key: str) -> Any:
    from a2a.client import ClientConfig, create_client

    hc = httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    return await create_client(st["base"], ClientConfig(streaming=False, httpx_client=hc)), hc


async def _send(client: Any, data: dict[str, Any]) -> Any:
    from a2a.helpers.proto_helpers import new_data_message
    from a2a.types.a2a_pb2 import Role, SendMessageRequest

    out = None
    async for ev in client.send_message(SendMessageRequest(message=new_data_message(data, role=Role.ROLE_USER))):
        out = ev
    assert out is not None and out.HasField("task")
    return out.task


@pytest.mark.asyncio
async def test_agent_card_and_partner_authentication(stack: dict[str, Any]) -> None:
    st = stack
    with httpx.Client() as h:
        card = h.get(f"{st['base']}/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["url"] == f"{st['base']}/a2a"
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert {s["id"] for s in card["skills"]} == {"subscription.status", "subscription.pause"}
        assert "partnerKey" in card["securitySchemes"]
        tcard = h.get(st["partner"]["agent_card"]).json()
        assert tcard["supportedInterfaces"][0]["tenant"] == st["demo"]["tenant_id"]
        anon = h.post(f"{st['base']}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}},
                      headers={"A2A-Version": "1.0"})
        assert anon.status_code == 401
        owner_key = h.post(f"{st['base']}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask",
                                                       "params": {"id": "x"}},
                           headers={"A2A-Version": "1.0", **st["owner"]})
        assert owner_key.status_code == 401                      # dashboard keys are not A2A partner keys


@pytest.mark.asyncio
async def test_status_pause_with_merchant_approval_and_isolation(stack: dict[str, Any], app_engine: Engine) -> None:
    from a2a.types.a2a_pb2 import GetTaskRequest, TaskState

    st = stack
    client, hc = await _client(st, st["partner"]["key"])
    try:
        status = await _send(client, {"skill": "subscription.status", "customer_ref": st["customer_ref"],
                                      "consent_ref": "consent-2026-10-01-77"})
        assert status.status.state == TaskState.TASK_STATE_COMPLETED
        assert status.artifacts and status.artifacts[0].parts[0].HasField("data")

        missing = await _send(client, {"skill": "subscription.pause", "customer_ref": st["customer_ref"]})
        assert missing.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

        pause = await _send(client, {"skill": "subscription.pause", "customer_ref": st["customer_ref"],
                                     "consent_ref": "consent-2026-10-01-78", "months": 2})
        assert pause.status.state == TaskState.TASK_STATE_WORKING              # waiting for the merchant
        assert (await client.get_task(GetTaskRequest(id=pause.id))).status.state == TaskState.TASK_STATE_WORKING
    finally:
        await client.close()
        await hc.aclose()

    # the merchant approves in the dashboard (Approvals) → the action executes → GetTask shows the outcome
    approvals = st["api"].get("/v1/approvals?status=pending", headers=st["owner"]).json()["items"]
    mine = [a for a in approvals if a["tool_name"] == "subscription.request_pause"]
    assert len(mine) == 1
    approver = {"Authorization": f"Bearer {st['demo']["api_keys"]["finance_approver"]}"}
    r = st["api"].post(f"/v1/approvals/{mine[0]['approval_id']}/decide", json={"grant": True}, headers=approver)
    assert r.status_code == 200, r.text
    client, hc = await _client(st, st["partner"]["key"])
    try:
        done = await client.get_task(GetTaskRequest(id=pause.id))
        assert done.status.state == TaskState.TASK_STATE_COMPLETED
    finally:
        await client.close()
        await hc.aclose()
    with tenant_tx(st["demo"]["tenant_id"], app_engine) as c:
        assert set(c.execute(text("SELECT status FROM billing.subscriptions WHERE customer_id=:c"),
                             {"c": st["customer_id"]}).scalars()) == {"paused"}

    # another partner of the same merchant cannot see these tasks
    client, hc = await _client(st, st["other"]["key"])
    try:
        from a2a.utils.errors import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):                    # not "forbidden": its existence is not revealed
            await client.get_task(GetTaskRequest(id=pause.id))
    finally:
        await client.close()
        await hc.aclose()
