"""Live voice recovery call end to end (P11, ADR-0026), with Exotel faked (no real call is ever placed in tests).

Policy first: without a registered calling header the call is refused. Registered: the gateway places the call
through Exotel carrying ONLY a signed token. The Voicebot stream connects with that token; the agent's first words
are the recording disclosure; the caller says "haan, kal pay kar dunga" → a promise for tomorrow (source: voice),
a WhatsApp payment link on the spot, an encrypted transcript, and Exotel's status callback closes the call.
A stream with a forged token is refused before a word is spoken.
"""

from __future__ import annotations

import array
import base64
import json
import math
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from starlette.websockets import WebSocketDisconnect

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
from nirantar.payments.providers.mock import MockProvider
from nirantar.settings import service as settings
from nirantar.voice import calls
from nirantar.voice.exotel import ExotelClient
from nirantar.voice.providers import StubSpeech

pytestmark = pytest.mark.integration
RATE = 8000


def _tone(ms: int) -> bytes:
    n = RATE * ms // 1000
    return array.array("h", (int(6000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n))).tobytes()


def _midday_zone() -> str:
    offset = 12 - datetime.now(UTC).hour
    offset = offset - 24 if offset > 14 else offset + 24 if offset < -12 else offset
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


class FakeExotel:
    def __init__(self) -> None:
        self.placed: list[dict[str, list[str]]] = []
        self.prefix = f"CA{uuid.uuid4().hex[:8]}"        # call SIDs are globally unique, like Exotel's

    def sid(self, n: int) -> str:
        return f"{self.prefix}{n:04d}"

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/Calls/connect.json")
        self.placed.append(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"Call": {"Sid": self.sid(len(self.placed)), "Status": "queued"}})


@pytest.fixture()
def world(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    t, mock, sink, fake = new_id("ten"), MockProvider(webhook_secret="whsec_v"), MockCommsSink(), FakeExotel()
    now = datetime.now(UTC)
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Ravi Tiffins", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec_v")
        cust = create_customer(c, t, NewCustomer("V-1", "Suresh Babu", "+919000006666", None, "hi",
                                                 consents={"whatsapp": True, "voice": True}))
        c.execute(text("UPDATE billing.customers SET timezone=:z"), {"z": _midday_zone()})
        sub = create_subscription(c, t, cust, "mock", mock.add_subscription(cust, Money.of("1200")), Money.of("1200"))
        debit = schedule_debit(c, t, sub, now.date(), now - timedelta(days=2))
        c.execute(text("UPDATE billing.debits SET status='failed' WHERE debit_id=:d"), {"d": debit})
    exotel = ExotelClient("acct_test", "key", "token", "api.exotel.com", "08012345678", "1353555",
                          client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES,
                     {"engine": app_engine, "provider": mock, "comms": sink, "voice": exotel})
    yield {"tenant": t, "customer": cust, "debit": debit, "gw": gw, "fake": fake, "sink": sink, "mock": mock}


def test_voice_call_promise_link_transcript_and_status(app_engine: Engine, owner_engine: Engine,
                                                       world: dict[str, Any]) -> None:
    w, t = world, world["tenant"]
    args = {"customer_id": w["customer"], "debit_id": w["debit"]}

    # ---- no registered calling header (TRAI): refused, nothing dialled
    refused = w["gw"].call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.place_call", args=args)
    assert refused.status == "denied" and w["fake"].placed == []
    with tenant_tx(t, app_engine) as c:
        settings.update(c, t, "policy", {"voice_registered": True}, actor="user:owner",
                        reason="1600-series header registered", now=datetime.now(UTC), expected_version=0)

    # ---- placed through Exotel, carrying only the signed token
    placed = w["gw"].call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.place_call", args=args,
                          idempotency_key=new_id("vcall"))
    assert placed.status == "executed", placed.error
    sent = w["fake"].placed[0]
    assert sent["From"] == ["+919000006666"] and sent["CallerId"] == ["08012345678"]
    assert sent["Url"] == ["http://my.exotel.com/acct_test/exoml/start_voice/1353555"]
    token = sent["CustomField"][0]
    assert calls.parse_token(token) == (t, placed.output["call_id"])
    assert "Ravi" not in token and "1200" not in token                 # nothing but the token travels

    # ---- the Voicebot stream: disclosure first, a spoken promise, a link on WhatsApp, an encrypted transcript
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine, extra={
        "voice_speech": StubSpeech(["haan bhai, kal pay kar dunga"]), "billing_gateway": w["gw"]}))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/exotel?token=" + token[:-4] + "0000") \
            as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"stream_sid": "s0", "call_sid": w["fake"].sid(1)}}))
        ws.receive_text()                                              # a forged token: closed, nothing said
    received = []
    with client.websocket_connect(f"/voice/exotel?token={token}") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({"event": "start", "start": {
            "stream_sid": "s1", "call_sid": w["fake"].sid(1),
            "custom_parameters": {"merchant": "EVIL CORP", "amount_text": "₹99,999", "language": "en"}}}))
        while (m := json.loads(ws.receive_text()))["event"] != "mark":
            received.append(m)
        speech = _tone(500) + b"\x00\x00" * (RATE * 800 // 1000)
        for i in range(0, len(speech), 3200):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": base64.b64encode(speech[i:i + 3200])
                                                                .decode()}}))
        while (m := json.loads(ws.receive_text()))["event"] != "stop":
            received.append(m)
    assert any(m["event"] == "media" for m in received)

    turns = calls.transcript(app_engine, t, placed.output["call_id"])
    assert turns[0]["role"] == "agent" and "Ravi Tiffins" in turns[0]["text"]          # server-side context only
    assert "EVIL CORP" not in json.dumps(turns, ensure_ascii=False)
    assert turns[1]["role"] == "caller" and turns[1]["intent"] == "promise_to_pay"
    with tenant_tx(t, app_engine) as c:
        call = c.execute(text("SELECT outcome, promise_id, transcript_enc FROM comms.calls WHERE call_id=:c"),
                         {"c": placed.output["call_id"]}).one()
        promise = c.execute(text("SELECT promised_date, source, quote FROM ops.promises WHERE promise_id=:p"),
                            {"p": call.promise_id}).one()
    assert call.outcome == "promise_to_pay" and b"kal pay" not in bytes(call.transcript_enc)     # encrypted at rest
    assert promise.promised_date == datetime.now(UTC).date() + timedelta(days=1) and promise.source == "voice_note"
    link_msgs = [(tp, msg) for tp, msg in zip(w["sink"].templates, w["sink"].messages, strict=True)
                 if tp and tp.key == "whatsapp.promise_reminder"]
    assert len(link_msgs) == 1 and "₹1,200.00" in link_msgs[0][1].text            # the link they asked for

    # ---- Exotel's terminal status callback closes the call record
    r = client.post(f"/webhooks/exotel/{t}", json={"CallSid": w["fake"].sid(1), "Status": "completed",
                                                   "ConversationDuration": "42"})
    assert r.status_code == 200 and r.json() == {"updated": 1}
    with tenant_tx(t, app_engine) as c:
        st = c.execute(text("SELECT status, duration_s FROM comms.calls WHERE call_id=:c"),
                       {"c": placed.output["call_id"]}).one()
    assert tuple(st) == ("completed", 42)
