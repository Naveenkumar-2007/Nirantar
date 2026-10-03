"""Voice gateway: Exotel Voicebot bidirectional stream (WebSocket) ↔ turn engine.

Inbound events (Exotel protocol): connected, start{stream_sid, call_sid, custom_parameters}, media{payload b64 PCM16
8 kHz}, dtmf, stop. Outbound: media{payload} chunks (3,200..100,000 bytes, multiple of 320), mark, clear.
Barge-in: if the caller speaks while we are still sending audio, we send `clear` to stop playback.
Call-level effects (send link, opt-out, human transfer) are delegated to `on_action`, which the worker
wires to MCP tools — the gateway itself never touches payments or the database. Scripts come from `scripts_for`
(the worker resolves the tenant's approved scripts from the template registry); without it, platform defaults.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from nirantar.voice.audio import EndOfUtterance, exotel_chunks
from nirantar.voice.providers import SpeechProvider
from nirantar.voice.turn import CallSession

OnAction = Callable[[dict[str, Any], str], None]
ScriptsFor = Callable[[dict[str, Any], str], dict[str, str]]     # (custom_parameters, language) → scripts


@dataclass
class CallRecord:
    call_sid: str
    language: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    barge_ins: int = 0


def build_app(speech_factory: Callable[[], SpeechProvider], on_action: OnAction,
              records: dict[str, CallRecord] | None = None, scripts_for: ScriptsFor | None = None) -> FastAPI:
    app = FastAPI(title="nirantar-voice-gateway")
    calls = records if records is not None else {}

    @app.websocket("/voice/exotel")
    async def exotel_stream(ws: WebSocket) -> None:
        await ws.accept()
        session: CallSession | None = None
        vad = EndOfUtterance(8000)
        stream_sid, record = "", None
        speaking_until = 0.0

        async def send_audio(pcm: bytes) -> None:
            nonlocal speaking_until
            for chunk in exotel_chunks(pcm):
                await ws.send_text(json.dumps({"event": "media", "stream_sid": stream_sid,
                                               "media": {"payload": base64.b64encode(chunk).decode()}}))
            speaking_until = time.monotonic() + len(pcm) / 16000.0
            await ws.send_text(json.dumps({"event": "mark", "stream_sid": stream_sid, "mark": {"name": "reply_end"}}))

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                event = msg.get("event")
                if event == "start":
                    start = msg["start"]
                    stream_sid = start.get("stream_sid", "")
                    params = start.get("custom_parameters", {})
                    lang = params.get("language", "en")
                    session = CallSession(language=lang, merchant=params.get("merchant", "your merchant"),
                                          amount_text=params.get("amount_text", ""), speech=speech_factory(),
                                          scripts=scripts_for(params, lang) if scripts_for else None)
                    record = CallRecord(start.get("call_sid", ""), session.language)
                    calls[record.call_sid] = record
                    text, pcm, _ = session.greeting()
                    record.turns.append({"role": "agent", "text": text})
                    await send_audio(pcm)
                elif event == "media" and session is not None and record is not None:
                    pcm = base64.b64decode(msg["media"]["payload"])
                    if time.monotonic() < speaking_until:
                        record.barge_ins += 1                  # caller talked over us: stop playback
                        speaking_until = 0.0
                        await ws.send_text(json.dumps({"event": "clear", "stream_sid": stream_sid}))
                    utterance = vad.feed(pcm)
                    if utterance:
                        result = session.turn(utterance)
                        record.turns.append({"role": "caller", "text": result.transcript_redacted,
                                             "intent": result.intent, "stt_ms": result.stt_ms})
                        record.turns.append({"role": "agent", "text": result.reply_text, "tts_ms": result.tts_ms})
                        if result.action != "none":
                            record.actions.append(result.action)
                            on_action({"call_sid": record.call_sid, "intent": result.intent}, result.action)
                        await send_audio(result.reply_pcm)
                        if session.ended:
                            await ws.send_text(json.dumps({"event": "stop", "stream_sid": stream_sid}))
                            await ws.close()
                            return
                elif event == "stop":
                    await ws.close()
                    return
        except WebSocketDisconnect:
            return

    return app
