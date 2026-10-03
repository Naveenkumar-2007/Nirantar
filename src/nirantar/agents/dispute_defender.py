"""Dispute Defender: build a verifiable evidence pack for a recurring-debit chargeback and draft a representment.

Evidence comes only from system-of-record facts (mandate, pre-debit notice timing, provider-verified payment,
consent/opt-out state). Each item is stored as a Unified Evidence object with a hash. The LLM writes prose
strictly from those facts; submission goes through the MCP gateway (approval above threshold).
Win probability (M11) is a transparent prior until labelled dispute outcomes exist — it is NOT a trained model.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from nirantar.disputes.evidence import EvidenceItem
from nirantar.llm.gateway import LLMGateway, LLMUnavailable
from nirantar.policy.engine import conduct_violations


def win_probability_prior(items: list[EvidenceItem]) -> float:
    """Transparent prior (M11 baseline) — replace with a trained model once dispute outcomes are labelled."""
    w = {"mandate_record": 0.25, "predebit_notice": 0.25, "payment_verification": 0.1, "no_prior_opt_out": 0.1}
    return round(min(0.9, 0.2 + sum(w.get(i.kind, 0) for i in items if i.supports_merchant)), 2)


class Representment(BaseModel):
    summary: str = Field(min_length=40, max_length=2000)


def draft_representment(items: list[EvidenceItem], llm: LLMGateway | None) -> tuple[str, str]:
    facts = [{"kind": i.kind, "supports_merchant": i.supports_merchant, **i.facts} for i in items]
    template = ("The customer authorised a recurring mandate; a pre-debit notification was sent before the debit; "
                "the debit was processed for the mandated amount; no opt-out was recorded. Evidence: "
                + "; ".join(f"{f['kind']}={'yes' if f['supports_merchant'] else 'no'}" for f in facts))
    if llm is None:
        return template, "template"
    try:
        out, _ = llm.complete_json(tier="strong", task="representment_draft", schema=Representment,
                                   system=("Write a factual chargeback representment for a card/UPI recurring debit. "
                                           "Use ONLY the facts given; do not invent documents or dates; neutral tone."),
                                   user="Draft the representment.", untrusted={"facts": json.dumps(facts)})
    except LLMUnavailable:
        return template, "template"
    if conduct_violations(out.summary):
        return template, "template"
    return out.summary, "llm"


def respond_by_ok(respond_by: datetime, now: datetime, margin: timedelta = timedelta(hours=24)) -> bool:
    return respond_by - now > margin
