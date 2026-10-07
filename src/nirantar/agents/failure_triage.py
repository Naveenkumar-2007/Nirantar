"""Failure Triage agent: classify a decline, decide retry vs customer contact.

Known reasons → rule table (deterministic, instant). Unknown → LLM (fast tier) with a strict schema.
LLM unavailable → UNKNOWN (no optional contact; flagged for review). A bank incident detected by M4
overrides everything: it's a technical failure, don't bother the customer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from nirantar.llm.gateway import LLMGateway, LLMUnavailable

Category = Literal["BANK_TECHNICAL", "INSUFFICIENT_FUNDS", "MANDATE_REVOKED", "CARD_EXPIRED", "LIMIT_EXCEEDED",
                   "NOT_PAID", "UNKNOWN"]

RULES: dict[str, str] = {
    "insufficient_balance": "INSUFFICIENT_FUNDS", "insufficient_funds": "INSUFFICIENT_FUNDS",
    "payment_declined_insufficient_funds": "INSUFFICIENT_FUNDS",
    "bank_technical_error": "BANK_TECHNICAL", "gateway_technical_error": "BANK_TECHNICAL",
    "server_error": "BANK_TECHNICAL", "timeout": "BANK_TECHNICAL", "bank_down": "BANK_TECHNICAL",
    "mandate_revoked": "MANDATE_REVOKED", "mandate_cancelled": "MANDATE_REVOKED", "token_cancelled": "MANDATE_REVOKED",
    "card_expired": "CARD_EXPIRED", "expired_card": "CARD_EXPIRED",
    "amount_exceeds_mandate_limit": "LIMIT_EXCEEDED", "mandate_limit_exceeded": "LIMIT_EXCEEDED",
    "not_paid_by_due_date": "NOT_PAID",     # pay-by-link: nothing was declined, the customer has not paid yet
}
RETRY = {"BANK_TECHNICAL": True, "INSUFFICIENT_FUNDS": True, "MANDATE_REVOKED": False, "CARD_EXPIRED": False,
         "LIMIT_EXCEEDED": False, "NOT_PAID": False, "UNKNOWN": False}
CONTACT = {"BANK_TECHNICAL": False, "INSUFFICIENT_FUNDS": True, "MANDATE_REVOKED": True, "CARD_EXPIRED": True,
           "LIMIT_EXCEEDED": True, "NOT_PAID": True, "UNKNOWN": False}


class TriageIn(BaseModel):
    error_code: str | None
    error_reason: str | None
    bank_degraded: bool = False


class LLMTriage(BaseModel):
    category: Category
    rationale: str


class TriageOut(BaseModel):
    category: Category
    retry_recommended: bool
    customer_contact_recommended: bool
    rationale: str
    source: Literal["rules", "llm", "fallback", "bank_health"]


def _out(category: str, rationale: str, source: str) -> TriageOut:
    return TriageOut(category=category, retry_recommended=RETRY[category],  # type: ignore[arg-type]
                     customer_contact_recommended=CONTACT[category], rationale=rationale,
                     source=source)  # type: ignore[arg-type]


def triage(inp: TriageIn, llm: LLMGateway | None = None) -> TriageOut:
    if inp.bank_degraded:
        return _out("BANK_TECHNICAL", "M4 reports an active incident at the customer's bank", "bank_health")
    reason = (inp.error_reason or "").strip().lower()
    if reason in RULES:
        return _out(RULES[reason], f"known decline reason '{reason}'", "rules")
    if llm is None:
        return _out("UNKNOWN", f"unmapped reason '{reason}', no LLM configured", "fallback")
    try:
        parsed, _ = llm.complete_json(
            tier="fast", task="failure_triage", schema=LLMTriage,
            system=("You classify failed recurring payment debits in India (UPI AutoPay, eNACH, card mandates). "
                    "Choose the single best category. If unsure, choose UNKNOWN."),
            user="Classify this decline.",
            untrusted={"provider_decline": f"error_code={inp.error_code}; error_reason={inp.error_reason}"})
    except LLMUnavailable:
        return _out("UNKNOWN", "LLM unavailable; deterministic fallback", "fallback")
    return _out(parsed.category, parsed.rationale[:300], "llm")
