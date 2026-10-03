"""The real MCP server over streamable HTTP with OAuth 2.1 (P7, ADR-0016): dynamic registration, PKCE, dashboard
consent, scoped tools through the ToolGateway, refresh rotation with reuse detection, revocation."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.comms.sink import MockCommsSink
from nirantar.db.session import tenant_tx
from nirantar.demo.seed import seed
from nirantar.payments.providers.mock import MockProvider

pytestmark = pytest.mark.integration
REDIRECT = "http://127.0.0.1:8765/callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def stack(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    import uvicorn

    from nirantar.mcp.server import asgi_app, build_server

    demo = seed(app_engine, n_customers=12, seed_value=7)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server, store = build_server(app_engine, base_url=base, consent_url="http://dashboard.test/connect/mcp",
                                 provider=MockProvider(), comms=MockCommsSink())
    uv = uvicorn.Server(uvicorn.Config(asgi_app(server, [f"127.0.0.1:{port}"]), host="127.0.0.1", port=port,
                                       log_level="warning"))
    th = threading.Thread(target=uv.run, daemon=True)
    th.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    api = TestClient(create_app(Services(engine=app_engine, owner_engine=owner_engine, provider=MockProvider())))
    yield {"base": base, "demo": demo, "api": api, "store": store}
    uv.should_exit = True
    th.join(5)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _connect(st: dict[str, Any], scopes: str, grant: list[str] | None) -> dict[str, Any]:
    """Register → authorize → dashboard consent → token. Returns the token response."""
    base, keys = st["base"], st["demo"]["api_keys"]
    with httpx.Client(base_url=base) as h:
        meta = h.get("/.well-known/oauth-authorization-server").json()
        reg = h.post(meta["registration_endpoint"], json={
            "client_name": "Test assistant", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "scope": scopes}).json()
        verifier, challenge = _pkce()
        r = h.get(meta["authorization_endpoint"], params={
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "scope": scopes,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"})
        assert r.status_code == 302 and r.headers["location"].startswith("http://dashboard.test/connect/mcp?request=")
        rid = parse_qs(urlparse(r.headers["location"]).query)["request"][0]

        shown = st["api"].get(f"/v1/mcp/requests/{rid}", headers={"Authorization": f"Bearer {keys['viewer']}"})
        assert shown.json()["client_name"] == "Test assistant"
        refused = st["api"].post(f"/v1/mcp/requests/{rid}/decide", json={"approve": True},
                                 headers={"Authorization": f"Bearer {keys['viewer']}"})
        assert refused.status_code == 403                          # only a tenant admin can connect a client
        d = st["api"].post(f"/v1/mcp/requests/{rid}/decide", json={"approve": True, "scopes": grant},
                           headers={"Authorization": f"Bearer {keys['owner']}"}).json()
        q = parse_qs(urlparse(d["redirect_url"]).query)
        assert q["state"] == ["xyz"]
        tok = h.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": reg["client_id"], "code_verifier": verifier}).json()
        again = h.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": reg["client_id"], "code_verifier": verifier})
        assert again.status_code == 400                            # codes are single use
    return {**tok, "client_id": reg["client_id"], "token_endpoint": meta["token_endpoint"]}


async def _session_call(base: str, token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    out: list[Any] = []
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as hc, \
            streamable_http_client(f"{base}/mcp", http_client=hc) as (read, write), \
            ClientSession(read, write) as session:
        await session.initialize()
        out.append(sorted(t.name for t in (await session.list_tools()).tools))
        for name, args in calls:
            out.append(await session.call_tool(name, args))
    return out


@pytest.mark.asyncio
async def test_mcp_client_connects_and_acts_through_the_gateway(stack: dict[str, Any], app_engine: Engine) -> None:
    st = stack
    t = st["demo"]["tenant_id"]
    with httpx.Client() as h:
        unauth = h.post(f"{st['base']}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert unauth.status_code == 401 and "resource_metadata" in unauth.headers.get("www-authenticate", "")

    tok = _connect(st, "nirantar:read nirantar:act nirantar:money", ["nirantar:read", "nirantar:money"])
    assert tok["scope"] == "nirantar:read nirantar:money"
    with tenant_tx(t, app_engine) as c:
        debit = c.execute(text("SELECT debit_id FROM billing.debits WHERE status='failed' LIMIT 1")).scalar_one()
        pending_before = c.execute(text("SELECT count(*) FROM ops.approvals WHERE status='pending'")).scalar_one()
    tools, overview, cases, denied, link = await _session_call(st["base"], tok["access_token"], [
        ("insights.overview", {}), ("cases.list_open", {"limit": 5}),
        ("comms.send_whatsapp", {"customer_id": "cus_x", "debit_id": debit, "text": "Hello, a quick note about your subscription payment."}),
        ("gateway.create_payment_link", {"debit_id": debit, "attempt": 1}),
    ])
    assert "treasury.request_credit_draw" not in tools and "experiment.assign_treatment" not in tools
    assert overview.is_error is False
    assert overview.structured_content["status"] == "executed"
    assert overview.structured_content["output"]["debits"]["total"] == 12
    assert cases.is_error is False
    assert denied.is_error is True and "nirantar:act" in denied.content[0].text      # scope not granted
    assert link.structured_content["status"] == "pending_approval"                    # money waits for a human
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT count(*) FROM ops.approvals WHERE status='pending'")).scalar_one() == \
            pending_before + 1
        agents = {r[0] for r in c.execute(text("SELECT DISTINCT agent_id FROM ops.actions WHERE agent_id LIKE "
                                               "'mcp_client:%'"))}
    assert agents == {f"mcp_client:{tok['client_id']}"}                              # attributed in the audit

    # the merchant approves the link in the dashboard → it executes with the MCP client's own tool set
    keys = st["demo"]["api_keys"]
    approval_id = link.structured_content["approval_id"]
    r = st["api"].post(f"/v1/approvals/{approval_id}/decide", json={"grant": True},
                       headers={"Authorization": f"Bearer {keys['finance_approver']}"}).json()
    assert r["executed"] is True and r["output"]["url"].startswith("https://")


def test_refresh_rotation_reuse_detection_and_revocation(stack: dict[str, Any]) -> None:
    st = stack
    tok = _connect(st, "nirantar:read", None)
    keys = st["demo"]["api_keys"]
    with httpx.Client() as h:
        def refresh(rt: str) -> httpx.Response:
            return h.post(tok["token_endpoint"], data={"grant_type": "refresh_token", "refresh_token": rt,
                                                       "client_id": tok["client_id"]})

        def ping(at: str) -> int:
            return h.post(f"{st['base']}/mcp", headers={"Authorization": f"Bearer {at}",
                                                        "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                              "protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}}}).status_code

        new = refresh(tok["refresh_token"]).json()
        assert new["access_token"] != tok["access_token"] and ping(new["access_token"]) == 200
        assert refresh(tok["refresh_token"]).status_code == 400          # reuse of a rotated token …
        assert ping(new["access_token"]) == 401                          # … revokes the whole grant

        tok2 = _connect(st, "nirantar:read", None)
        grants = st["api"].get("/v1/mcp/grants", headers={"Authorization": f"Bearer {keys['viewer']}"}).json()
        active = [g for g in grants["items"] if g["revoked_at"] is None and g["client_id"] == tok2["client_id"]]
        assert len(active) == 1
        r = st["api"].delete(f"/v1/mcp/grants/{active[0]['grant_id']}",
                             headers={"Authorization": f"Bearer {keys['owner']}"})
        assert r.status_code == 200 and ping(tok2["access_token"]) == 401
