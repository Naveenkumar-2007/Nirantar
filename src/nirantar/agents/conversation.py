"""Conversation Agent: draft a short, compliant recovery message in the customer's language.

The LLM writes the wording only. Facts are injected deterministically: the amount string comes from
Money formatting, the link from the payment-link tool. The draft must contain the exact amount string
and the {link} placeholder; otherwise the tenant's approved template (passed in by the caller from the template
registry via the `content.get_template` tool) is used. The Compliance Guardian screens
the final text again at the MCP gateway (conduct rules), and the send tool re-checks every ₹ figure.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from nirantar.core.money import Money
from nirantar.llm.gateway import LLMGateway, LLMUnavailable
from nirantar.policy.engine import conduct_violations

LANG_NAMES = {"en": "English", "hi": "Hindi", "te": "Telugu"}


class DraftIn(BaseModel):
    customer_name: str
    language: str
    amount: Money
    plan_name: str
    failure_category: str
    template: str                     # approved body with {name} {amount} {plan} {link} placeholders
    template_ref: str                 # e.g. whatsapp.recovery@te:v3 — recorded with the message
    template_language: str            # may differ from `language` when the registry fell back to English

    model_config = {"arbitrary_types_allowed": True}


class LLMDraft(BaseModel):
    text: str = Field(min_length=20, max_length=600)
    language: Literal["en", "hi", "te"]


class DraftOut(BaseModel):
    text_with_placeholder: str
    language: str
    source: Literal["llm", "template"]
    template_ref: str | None = None


_SCRIPT_RANGES = {"te": (0x0C00, 0x0C7F), "hi": (0x0900, 0x097F)}  # Telugu, Devanagari


def in_expected_script(text: str, language: str) -> bool:
    """A Telugu/Hindi draft must be mostly in that script (live test: an LLM answered in English)."""
    rng = _SCRIPT_RANGES.get(language)
    if rng is None:
        return True
    letters = [ch for ch in text if ch.isalpha()]
    native = sum(1 for ch in letters if rng[0] <= ord(ch) <= rng[1])
    return bool(letters) and native / len(letters) >= 0.5


def format_amount(amount: Money) -> str:
    return f"₹{amount.to_decimal():,.2f}"


def template_draft(inp: DraftIn) -> DraftOut:
    values = {"name": inp.customer_name, "amount": format_amount(inp.amount), "plan": inp.plan_name}
    text = re.sub(r"\{(name|amount|plan)\}", lambda m: values[m.group(1)], inp.template)
    return DraftOut(text_with_placeholder=text, language=inp.template_language, source="template",
                    template_ref=inp.template_ref)


def draft(inp: DraftIn, llm: LLMGateway | None = None) -> DraftOut:
    amount_str = format_amount(inp.amount)
    lang = inp.language if inp.language in LANG_NAMES else "en"
    if llm is None:
        return template_draft(inp)
    try:
        parsed, _ = llm.complete_json(
            tier="indic" if lang in ("hi", "te") else "fast", task="recovery_message_draft", schema=LLMDraft,
            system=("You write short, polite payment-reminder messages for an Indian subscription business. "
                    "Rules: no threats, no urgency pressure, no discounts or offers, no mention of anyone other "
                    "than the customer, max 3 sentences. Use the amount string EXACTLY as given and include the "
                    "literal placeholder {link} once."),
            user=(f"Language: {LANG_NAMES[lang]} (code {lang}). Customer first name: {inp.customer_name}. "
                  f"Plan: {inp.plan_name}. Amount string: {amount_str}. Reason category: {inp.failure_category}."),
            max_tokens=400)
    except LLMUnavailable:
        return template_draft(inp)
    text = parsed.text.strip()
    if (amount_str not in text or text.count("{link}") != 1 or conduct_violations(text)
            or not in_expected_script(text, lang)):
        return template_draft(inp)  # the model broke a hard rule: fall back, don't "fix" it
    return DraftOut(text_with_placeholder=text, language=lang, source="llm")
