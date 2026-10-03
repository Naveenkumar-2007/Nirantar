"""Voice turn engine: caller utterance → STT → safety screen → intent → scripted, compliant reply → TTS.

Replies are the tenant's approved scripts per language from the template registry (no free LLM speech on money
calls in v1). Safety rules:
- The agent never asks for OTPs/PINs. If the caller starts reading one out, it is redacted from the stored
  transcript and the caller is told never to share it.
- Hardship, distress or complaint → immediate transfer to a human.
- Recording disclosure is the first thing said on every call (ADR-0004 e).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.engine import Connection

from nirantar.settings import templates
from nirantar.voice.providers import SpeechProvider

Action = Literal["none", "send_link", "transfer_to_human", "record_opt_out", "end_call"]

VOICE_KEYS = ("greeting", "promise_to_pay", "hardship", "opt_out", "otp_guard", "reprompt", "goodbye")


def platform_scripts(language: str) -> dict[str, str]:
    """Platform default scripts (settings/defaults/templates.yaml), English where a language has none."""
    out = {}
    for k in VOICE_KEYS:
        body = templates.platform_default(f"voice.{k}", language) or templates.platform_default(f"voice.{k}", "en")
        assert body is not None, f"missing platform voice script voice.{k}"
        out[k] = body
    return out


def tenant_scripts(conn: Connection, tenant_id: str, language: str) -> dict[str, str]:
    """The tenant's approved scripts (template registry, maker-checker), falling back to platform defaults."""
    return {k: templates.resolve(conn, tenant_id, f"voice.{k}", language).body for k in VOICE_KEYS}


_OTP_WORDS = re.compile(r"\b(otp|pin|cvv|one[- ]time|password)\b|ఓటీపీ|పిన్|ओटीपी|पिन", re.IGNORECASE)
_DIGITS = re.compile(r"(?:\d[\s-]?){4,8}")
_YES = re.compile(r"\b(yes|ok(ay)?|send|haan|ha|sure|will pay|pay)\b|అవును|సరే|పంపండి|హా|हाँ|हां|भेज|ठीक|करूंगा|చెల్లిస్తాను",
                  re.IGNORECASE)
_HARDSHIP = re.compile(r"\b(lost (my )?job|hospital|medical|can'?t afford|hardship|emergency|complain)\b|"
                       r"ఉద్యోగం పోయింది|ఆసుపత్రి|नौकरी चली|अस्पताल|शिकायत", re.IGNORECASE)
_STOP = re.compile(r"\b(stop|don'?t call|do not call)\b|కాల్ చేయకండి|कॉल मत", re.IGNORECASE)


def redact_secrets(text: str) -> tuple[str, bool]:
    if _OTP_WORDS.search(text) or _DIGITS.search(text):
        redacted = _DIGITS.sub("[REDACTED]", text)
        return redacted, bool(_OTP_WORDS.search(text)) or redacted != text
    return text, False


def intent_of(text: str) -> str:
    if _HARDSHIP.search(text):
        return "hardship"
    if _STOP.search(text):
        return "opt_out"
    if _YES.search(text):
        return "promise_to_pay"
    return "other"


@dataclass
class TurnResult:
    transcript_redacted: str
    intent: str
    reply_key: str
    reply_text: str
    action: Action
    reply_pcm: bytes
    stt_ms: int
    tts_ms: int
    secret_detected: bool


@dataclass
class CallSession:
    language: str
    merchant: str
    amount_text: str
    speech: SpeechProvider
    scripts: dict[str, str] | None = None     # approved scripts for this language; None → platform defaults
    script_language: str | None = None        # language the scripts are written in (TTS voice); default derived
    rate: int = 8000
    reprompts: int = 0
    transcript_log: list[str] = field(default_factory=list)
    ended: bool = False

    def __post_init__(self) -> None:
        if self.scripts is None:
            self.scripts = platform_scripts(self.language)
        if self.script_language is None:
            has_own = templates.platform_default("voice.greeting", self.language) is not None
            self.script_language = self.language if has_own else "en"

    def _say(self, key: str) -> tuple[str, bytes, int]:
        assert self.scripts is not None
        text = templates.render(self.scripts[key], {"merchant": self.merchant, "amount": self.amount_text})
        pcm, ms = self.speech.tts(text, self.script_language or self.language, self.rate)
        return text, pcm, ms

    def greeting(self) -> tuple[str, bytes, int]:
        return self._say("greeting")

    def turn(self, utterance_pcm: bytes) -> TurnResult:
        tr = self.speech.stt(utterance_pcm, self.rate, self.language)
        clean, secret = redact_secrets(tr.text)
        self.transcript_log.append(clean)             # only the redacted text is ever stored
        intent = "otp_guard" if secret else intent_of(clean)
        action: Action = "none"
        if intent == "promise_to_pay":
            key, action, self.ended = "promise_to_pay", "send_link", True
        elif intent == "hardship":
            key, action, self.ended = "hardship", "transfer_to_human", True
        elif intent == "opt_out":
            key, action, self.ended = "opt_out", "record_opt_out", True
        elif intent == "otp_guard":
            key = "otp_guard"
        elif self.reprompts < 1:
            key, self.reprompts = "reprompt", self.reprompts + 1
        else:
            key, action, self.ended = "goodbye", "end_call", True
        text, pcm, tts_ms = self._say(key)
        return TurnResult(clean, intent, key, text, action, pcm, tr.latency_ms, tts_ms, secret)
