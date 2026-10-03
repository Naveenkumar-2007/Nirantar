from __future__ import annotations

import array
import base64
import json
import math

import pytest
from fastapi.testclient import TestClient

from nirantar.voice.audio import EXOTEL_ALIGN, EXOTEL_MIN_CHUNK, EndOfUtterance, exotel_chunks
from nirantar.voice.gateway import CallRecord, build_app
from nirantar.voice.providers import StubSpeech
from nirantar.voice.turn import CallSession, intent_of, redact_secrets

RATE = 8000


def tone(ms: int, amp: int = 6000) -> bytes:
    n = RATE * ms // 1000
    return array.array("h", (int(amp * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n))).tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (RATE * ms // 1000)


def test_exotel_chunks_are_protocol_compliant() -> None:
    chunks = exotel_chunks(tone(1234))
    assert all(len(c) >= EXOTEL_MIN_CHUNK and len(c) % EXOTEL_ALIGN == 0 for c in chunks)
    with pytest.raises(ValueError):
        exotel_chunks(b"x", chunk=1000)


def test_end_of_utterance_detection() -> None:
    vad = EndOfUtterance(RATE)
    assert vad.feed(silence(500)) is None                   # silence alone is not speech
    assert vad.feed(tone(400)) is None                      # speaking
    utt = vad.feed(silence(700))                            # ≥600 ms quiet ends the utterance
    assert utt is not None and len(utt) >= len(tone(400))
    assert vad.feed(tone(60) + silence(700)) is None        # too short to be speech (a click)


@pytest.mark.parametrize(("text", "intent"), [
    ("haan, link bhejo", "promise_to_pay"), ("అవును, లింక్ పంపండి", "promise_to_pay"),
    ("मैंने नौकरी चली गई है", "hardship"), ("I lost my job last month", "hardship"),
    ("please don't call me again", "opt_out"), ("what?", "other"),
])
def test_intents_across_languages(text: str, intent: str) -> None:
    assert intent_of(text) == intent


def test_otp_is_redacted_and_never_stored() -> None:
    clean, secret = redact_secrets("my OTP is 4 5 6 7 8 9")
    assert secret and "4 5 6" not in clean and "[REDACTED]" in clean
    session = CallSession("en", "Chai Club", "₹999", StubSpeech(["my OTP is 482913"]))
    r = session.turn(tone(500))
    assert r.intent == "otp_guard" and r.action == "none" and "482913" not in session.transcript_log[0]
    assert "never" in r.reply_text.lower()


def test_hardship_transfers_to_human_and_reprompt_then_end() -> None:
    s = CallSession("te", "Chai Club", "₹999", StubSpeech(["ఉద్యోగం పోయింది"]))
    assert s.turn(tone(500)).action == "transfer_to_human" and s.ended
    s2 = CallSession("en", "Chai Club", "₹999", StubSpeech(["hmm", "what"]))
    assert s2.turn(tone(500)).reply_key == "reprompt"
    assert s2.turn(tone(500)).action == "end_call"


def _exotel_call(client: TestClient, transcripts: list[str], language: str = "te") -> list[dict]:
    received = []
    with client.websocket_connect("/voice/exotel") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({"event": "start", "start": {
            "stream_sid": "st_1", "call_sid": "call_1",
            "custom_parameters": {"language": language, "merchant": "Chai Club", "amount_text": "₹999"}}}))
        # greeting audio comes back first
        while True:
            m = json.loads(ws.receive_text())
            received.append(m)
            if m["event"] == "mark":
                break
        speech = tone(500) + silence(800)
        for i in range(0, len(speech), 3200):
            ws.send_text(json.dumps({"event": "media", "media": {
                "payload": base64.b64encode(speech[i:i + 3200]).decode()}}))
        while True:
            m = json.loads(ws.receive_text())
            received.append(m)
            if m["event"] == "stop":
                break
    return received


def test_exotel_stream_end_to_end_with_stub_speech() -> None:
    records: dict[str, CallRecord] = {}
    actions: list[tuple[str, str]] = []
    stub = StubSpeech(["అవును, లింక్ పంపండి"])
    app = build_app(lambda: stub, lambda ctx, action: actions.append((ctx["call_sid"], action)), records)
    msgs = _exotel_call(TestClient(app), ["అవును"])
    media = [base64.b64decode(m["media"]["payload"]) for m in msgs if m["event"] == "media"]
    assert media and all(len(p) % 320 == 0 and len(p) >= 3200 for p in media)
    assert actions == [("call_1", "send_link")]
    rec = records["call_1"]
    assert rec.turns[0]["role"] == "agent" and "రికార్డ్" in rec.turns[0]["text"]     # recording disclosure first
    assert rec.turns[1]["intent"] == "promise_to_pay"
    assert rec.barge_ins >= 1                              # we spoke straight over the greeting in this test
