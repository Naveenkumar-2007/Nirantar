"""WhatsApp channel end to end against a fake Graph API (P6, ADR-0017): 24-hour window and approved templates,
signed webhooks, inbound routing, Sarvam voice notes with OTP redaction, screenshots, STOP, statuses, spoken
acknowledgements, template submission. The real account is exercised separately (live check, opt-in)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.channels.inbound import process_whatsapp
from nirantar.channels.sink import ChannelSink, TemplateNotApproved
from nirantar.channels.whatsapp import WhatsAppCloud
from nirantar.comms.sink import OutboundTemplate
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.mock import MockProvider
from nirantar.voice.providers import StubSpeech

pytestmark = pytest.mark.integration
SECRET = "test-app-secret"
NUMBER = "919000012345"


class FakeGraph:
    """Just enough of the Graph API: records every request and answers like Meta does."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body: Any = None
        if req.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(req.content)
        self.requests.append((req.method, path, body))
        if path.endswith("/messages"):
            self.n += 1
            return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"wa_id": body["to"]}],
                                             "messages": [{"id": f"wamid.{new_id('msg')}"}]})
        if path.endswith("/media") and req.method == "POST":
            return httpx.Response(200, json={"id": "media.out1"})
        if path.endswith("/media.in1"):
            return httpx.Response(200, json={"url": "https://lookaside.fake/media.in1", "mime_type": "audio/ogg"})
        if "lookaside.fake" in str(req.url):
            return httpx.Response(200, content=b"OggS-fake-voice-note", headers={"content-type": "audio/ogg"})
        if path.endswith("/message_templates") and req.method == "GET":
            return httpx.Response(200, json={"data": self.templates})
        if path.endswith("/message_templates") and req.method == "POST":
            self.templates.append({"name": body["name"], "language": body["language"], "status": "PENDING",
                                   "category": body["category"], "id": f"t{len(self.templates)}"})
            return httpx.Response(200, json={"id": f"t{len(self.templates)}", "status": "PENDING"})
        return httpx.Response(404, json={"error": {"code": 100, "message": f"unknown {path}"}})

    def sent(self) -> list[dict[str, Any]]:
        return [b for m, p, b in self.requests if m == "POST" and p.endswith("/messages")]


@pytest.fixture()
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture()
def wa(graph: FakeGraph) -> WhatsAppCloud:
    return WhatsAppCloud("1234", "token", "waba1", client=httpx.Client(transport=httpx.MockTransport(graph.handler)))


@pytest.fixture()
def merchant(app_engine: Engine, owner_engine: Engine) -> Iterator[dict[str, Any]]:
    t, now = new_id("ten"), datetime.now(UTC)
    mock = MockProvider()
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Chai Club", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:unused", "literal:whsec")
        cust = create_customer(c, t, NewCustomer(new_id("ext"), "Lakshmi Rao", f"+{NUMBER}", None, "hi",
                                                 consents={"whatsapp": True}))
        sub = create_subscription(c, t, cust, "mock", mock.add_subscription(cust, Money.of("499")), Money.of("499"))
        debit = schedule_debit(c, t, sub, (now + timedelta(days=2)).date(), now)
        c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, opened_at) "
                       "VALUES (:t, :c, 'debit_cycle', :d, :cu, 'open', :n)"),
                  {"t": t, "c": new_id("cas"), "d": debit, "cu": cust, "n": now})
    # this test suite owns the deployment-wide template table rows it creates
    with owner_engine.begin() as c:
        c.execute(text("DELETE FROM config.whatsapp_templates"))
    yield {"tenant": t, "customer": cust, "debit": debit, "mock": mock}
    with owner_engine.begin() as c:
        c.execute(text("DELETE FROM config.whatsapp_templates"))


def _webhook(messages: list[dict[str, Any]] | None = None, statuses: list[dict[str, Any]] | None = None) -> bytes:
    value: dict[str, Any] = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1234"}}
    if messages:
        value["contacts"] = [{"wa_id": NUMBER, "profile": {"name": "Lakshmi"}}]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return json.dumps({"object": "whatsapp_business_account",
                       "entry": [{"id": "waba1", "changes": [{"field": "messages", "value": value}]}]}).encode()


def _ts() -> str:
    return str(int(datetime.now(UTC).timestamp()))


def test_window_templates_inbound_voice_stop_and_statuses(app_engine: Engine, graph: FakeGraph, wa: WhatsAppCloud,
                                                          merchant: dict[str, Any]) -> None:
    t, cust = merchant["tenant"], merchant["customer"]
    sink = ChannelSink(app_engine, wa, StubSpeech(["mera OTP 482913 hai, main kal pay karunga"]))
    tpl = OutboundTemplate("whatsapp.recovery", "hi", {"name": "Lakshmi", "amount": "₹499.00", "plan": "Chai Club",
                                                       "link": "https://rzp.io/i/abc"})
    now = datetime.now(UTC)
    with pytest.raises(TemplateNotApproved):                     # no approved template, no conversation yet
        sink.send("whatsapp", cust, "free text", now, tenant_id=t, template=tpl)
    assert graph.sent() == []

    with app_engine.begin() as c:                                # Meta approved the Hindi template
        c.execute(text("INSERT INTO config.whatsapp_templates (template_key, language, meta_name, meta_language, "
                       "category, params, status) VALUES ('whatsapp.recovery', 'hi', 'nirantar_payment_retry', 'hi', "
                       "'UTILITY', ARRAY['name','plan','amount','link'], 'APPROVED')"))
    mid = sink.send("whatsapp", cust, "free text", now, tenant_id=t, template=tpl)
    sent = graph.sent()[-1]
    assert sent["to"] == NUMBER and sent["type"] == "template"
    assert [p["text"] for p in sent["template"]["components"][0]["parameters"]] == \
        ["Lakshmi", "Chai Club", "₹499.00", "https://rzp.io/i/abc"]          # Hindi body order, not English

    # statuses: delivered → read; a late "delivered" never moves it back
    for st in ("delivered", "read", "delivered"):
        process_whatsapp(app_engine, json.loads(_webhook(statuses=[{"id": mid, "status": st, "timestamp": _ts(),
                                                                     "recipient_id": NUMBER}])), wa=wa)
    with tenant_tx(t, app_engine) as c:
        assert c.execute(text("SELECT status FROM comms.messages WHERE message_id=:m"), {"m": mid}).scalar_one() == \
            "read"

    # the customer answers with a voice note → Sarvam STT → OTP redacted → reply event on their open debit
    res = process_whatsapp(app_engine, json.loads(_webhook(messages=[{
        "from": NUMBER, "id": "wamid.in1", "timestamp": _ts(), "type": "audio",
        "audio": {"id": "media.in1", "mime_type": "audio/ogg; codecs=opus", "voice": True}}])), wa=wa,
        speech=sink.speech)
    assert res.messages == 1
    with tenant_tx(t, app_engine) as c:
        ev = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type='reply.received'")).scalar_one()
        evd = c.execute(text("SELECT extracted_fields FROM ai.evidence WHERE modality='voice_note'")).scalar_one()
    payload = (ev if isinstance(ev, dict) else json.loads(ev))["payload"]
    assert payload["debit_id"] == merchant["debit"] and payload["via"] == "audio"
    assert "482913" not in payload["text"] and "kal pay" in payload["text"]
    assert (evd if isinstance(evd, dict) else json.loads(evd))["secret_detected"] is True

    # duplicate webhook delivery (Meta retries) changes nothing
    again = process_whatsapp(app_engine, json.loads(_webhook(messages=[{
        "from": NUMBER, "id": "wamid.in1", "timestamp": _ts(), "type": "audio",
        "audio": {"id": "media.in1", "mime_type": "audio/ogg"}}])), wa=wa, speech=sink.speech)
    assert again.duplicates == 1 and again.messages == 0

    # inside the 24 h window now: free text is allowed (no template needed)
    sink.send("whatsapp", cust, "Thanks, noted.", datetime.now(UTC), tenant_id=t)
    assert graph.sent()[-1]["type"] == "text"

    # STOP opts out immediately; the Guardian then refuses optional WhatsApp contact
    process_whatsapp(app_engine, json.loads(_webhook(messages=[{
        "from": NUMBER, "id": "wamid.in2", "timestamp": _ts(), "type": "text", "text": {"body": "STOP"}}])), wa=wa)
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "comms": sink,
                                                        "provider": merchant["mock"]})
    r = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.send_whatsapp",
                args={"customer_id": cust, "debit_id": merchant["debit"], "text": "Your payment is pending."})
    assert r.status == "denied"

    # a number we never messaged is not routed
    assert process_whatsapp(app_engine, json.loads(_webhook(messages=[{
        "from": "919999999999", "id": "wamid.x", "timestamp": _ts(), "type": "text", "text": {"body": "hi"}}])),
        wa=wa).unmatched == 1


def test_spoken_acknowledgement_through_the_gateway(app_engine: Engine, graph: FakeGraph, wa: WhatsAppCloud,
                                                     merchant: dict[str, Any]) -> None:
    t, cust = merchant["tenant"], merchant["customer"]
    speech = StubSpeech([])
    sink = ChannelSink(app_engine, wa, speech)
    process_whatsapp(app_engine, json.loads(_webhook(messages=[{     # unknown sender until we message them
        "from": NUMBER, "id": "wamid.pre", "timestamp": _ts(), "type": "text", "text": {"body": "hello"}}])), wa=wa)
    with app_engine.begin() as c:                                    # we messaged them before; they wrote back
        c.execute(text("INSERT INTO comms.wa_routes (phone_hash, tenant_id, customer_id, last_outbound_at, "
                       "last_inbound_at) VALUES (:h, :t, :c, now(), now())"),
                  {"h": __import__("nirantar.channels.sink", fromlist=["phone_hash"]).phone_hash(NUMBER), "t": t,
                   "c": cust})
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "comms": sink,
                                                        "provider": merchant["mock"]})
    r = gw.call(tenant_id=t, agent_id="conductor", tool_name="comms.acknowledge_reply",
                args={"customer_id": cust, "intent": "promise_to_pay",
                      "promised_date": (datetime.now(UTC).date() + timedelta(days=1)).isoformat(), "via": "audio"})
    assert r.status == "executed", r
    kinds = [b["type"] for b in graph.sent()]
    assert kinds == ["text", "audio"]                                 # written + spoken, both in Hindi
    assert "धन्यवाद" in graph.sent()[0]["text"]["body"] and speech.spoken and "धन्यवाद" in speech.spoken[0]
    assert r.output["voice_ref"] and r.output["template_ref"].startswith("whatsapp.reply_ack_promise@hi")


def test_webhook_endpoint_signature_and_handshake(app_engine: Engine, owner_engine: Engine) -> None:
    from nirantar.api.app import create_app
    from nirantar.api.deps import Services
    from nirantar.comms.sink import MockCommsSink

    old = {k: os.environ.get(k) for k in ("WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")}
    os.environ["WHATSAPP_APP_SECRET"], os.environ["WHATSAPP_VERIFY_TOKEN"] = SECRET, "verify-me"
    try:
        api = TestClient(create_app(Services(engine=app_engine, owner_engine=owner_engine, comms=MockCommsSink())))
        ok = api.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                   "hub.challenge": "12345"})
        assert ok.status_code == 200 and ok.text == "12345"
        assert api.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                     "hub.challenge": "1"}).status_code == 403
        body = _webhook(statuses=[{"id": "wamid.unknown", "status": "delivered", "timestamp": _ts()}])
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert api.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
        assert api.post("/webhooks/whatsapp", content=body,
                        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_template_submission(app_engine: Engine, owner_engine: Engine, graph: FakeGraph, wa: WhatsAppCloud) -> None:
    from nirantar.channels import wa_templates

    with owner_engine.begin() as c:
        c.execute(text("DELETE FROM config.whatsapp_templates"))
    out = wa_templates.submit(app_engine, wa)
    assert len(out) == len(wa_templates.CATALOG) * 3 and all(r.get("status") == "PENDING" for r in out)
    bodies = [b for m, p, b in graph.requests if m == "POST" and p.endswith("/message_templates")]
    retry = next(b for b in bodies if b["name"] == "nirantar_payment_retry" and b["language"] == "en")
    assert retry["components"][0]["text"].endswith("Thank you.")          # never ends with a variable
    assert retry["components"][0]["example"]["body_text"][0][0] == "Priya"
    assert wa_templates.submit(app_engine, wa) == []                        # nothing submitted twice
    with app_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM config.whatsapp_templates WHERE status='PENDING'")).scalar_one()             == len(out)
    with owner_engine.begin() as c:
        c.execute(text("DELETE FROM config.whatsapp_templates"))
