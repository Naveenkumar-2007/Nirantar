"""AI guardrails, round 2 (ADR-0027): what untrusted text may carry into an LLM call.

Defence in depth around the LLM gateway (round 1 already fences untrusted text, validates structured output, falls
back deterministically, and the LLM can never act — only the MCP gateway can):

1. Data minimisation   — before any third-party model sees customer text: phone numbers, emails, card numbers,
                          Aadhaar, PAN, UPI IDs, IFSC/account numbers and OTP-like codes are replaced by typed
                          placeholders. The model never needs them to classify a reply or draft a message.
2. Injection detection — instruction-override phrases (English, Hinglish, Hindi, Telugu), role/system spoofing,
                          tool/function-call bait, fence-escape attempts, and hidden characters (zero-width,
                          bidi overrides, tag characters). A finding never blocks a money workflow; it labels the
                          text for the model, is recorded with the call, and pushes callers to their deterministic path.
3. Normalisation       — invisible and bidi-control characters are removed before the text is fenced, so what the
                          model sees is what a human reviewer sees.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 1. redaction (order matters: specific → general)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("UPI_ID", re.compile(r"\b[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("AADHAAR", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("PHONE", re.compile(r"(?:\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")),
    ("ACCOUNT", re.compile(r"\b\d{9,18}\b")),
    ("OTP", re.compile(r"\b(?:otp|pin|code|cvv)\W{0,3}\d{3,8}\b|\b\d{4,8}\b(?=\W{0,3}(?:otp|pin|is my otp))",
                       re.IGNORECASE)),
)


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    out = text
    for label, pat in _PATTERNS:
        out, n = pat.subn(f"[{label}]", out)
        if n:
            counts[label] = counts.get(label, 0) + n
    return out, counts


# ---------------------------------------------------------------- 2. injection detection
_INJECTION: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system|the)\b"
        r".{0,30}\b(instructions?|rules?|prompts?|guidelines?|policies|polic(y|ies))\b", re.IGNORECASE | re.DOTALL)),
    ("new_instructions", re.compile(r"\b(new|updated|real|actual)\s+(instructions?|rules|task)\s*[:\-]", re.I)),
    ("role_spoof", re.compile(r"(^|\n)\s*(system|assistant|developer|admin|root)\s*[:>]|<\|?(im_start|system|endoftext)"
                              r"\|?>|\[/?(INST|SYS)\]", re.IGNORECASE)),
    ("persona", re.compile(r"\byou are (now|no longer)\b|\bact as\b|\bpretend (to be|you are)\b|\bjailbreak\b|"
                           r"\bDAN\b|developer mode", re.IGNORECASE)),
    ("tool_bait", re.compile(r"\b(call|invoke|use|run|execute)\b.{0,20}\b(tool|function|api|refund|payout|transfer|"
                             r"waive|discount|write[ -]?off)\b|\"?(function_call|tool_calls?)\"?\s*:", re.IGNORECASE)),
    ("exfiltrate", re.compile(r"\b(reveal|print|show|repeat|leak|send)\b.{0,30}\b(system prompt|instructions|api key|"
                              r"secret|token|password|other customers?)\b", re.IGNORECASE)),
    ("fence_escape", re.compile(r"</?\s*(untrusted|data|document|context)[^>]*>|```\s*(system|end)|"
                                r"END OF (DATA|DOCUMENT|UNTRUSTED)", re.IGNORECASE)),
    ("hinglish", re.compile(r"\b(pichl[ae]|purane|saare)\s+(instructions?|niyam|rules)\s+(bhool|ignore|chhod)|"
                            r"\b(niyam|instructions?)\s+(bhool\s*jao|ignore\s*karo|mat\s*mano)", re.IGNORECASE)),
    ("hindi", re.compile(r"(पिछले|सारे|सभी)\s*(निर्देश|नियम)\s*(भूल|अनदेखा)|निर्देशों\s*को\s*(भूल|अनदेखा)")),
    ("telugu", re.compile(r"(ముందు|అన్ని)\s*(సూచనలు|నియమాలు)\s*(మర్చిపో|పట్టించుకోవద్దు)")),
)
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")


@dataclass(frozen=True)
class Finding:
    rules: tuple[str, ...] = ()
    hidden_chars: int = 0
    redactions: dict[str, int] = field(default_factory=dict)

    @property
    def suspicious(self) -> bool:
        return bool(self.rules) or self.hidden_chars > 0


def normalise(text: str) -> tuple[str, int]:
    """NFKC (full-width / look-alike forms collapse) and remove invisible / bidi-control / tag characters."""
    hidden = len(_HIDDEN.findall(text))
    return unicodedata.normalize("NFKC", _HIDDEN.sub("", text)), hidden


def inspect(text: str) -> tuple[str, Finding]:
    """Clean untrusted text for an LLM: normalise, detect injection, redact personal data."""
    clean, hidden = normalise(text or "")
    rules = tuple(name for name, pat in _INJECTION if pat.search(clean))
    redacted, counts = redact(clean)
    return redacted, Finding(rules, hidden, counts)
