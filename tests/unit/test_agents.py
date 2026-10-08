from __future__ import annotations

import os

import pytest

from nirantar.agents import conversation, debit_strategist, failure_triage
from nirantar.agents.specs import SPECS
from nirantar.core.money import Money
from nirantar.llm.gateway import LLMGateway, StubProvider
from nirantar.settings.templates import platform_default


def test_triage_rules_bank_override_and_fallback() -> None:
    t = failure_triage.triage(failure_triage.TriageIn(error_code="X", error_reason="insufficient_balance"))
    assert (t.category, t.source, t.customer_contact_recommended) == ("INSUFFICIENT_FUNDS", "rules", True)
    b = failure_triage.triage(failure_triage.TriageIn(error_code="X", error_reason="insufficient_balance",
                                                      bank_degraded=True))
    assert b.category == "BANK_TECHNICAL" and not b.customer_contact_recommended
    u = failure_triage.triage(failure_triage.TriageIn(error_code="X", error_reason="weird_new_code"))
    assert u.category == "UNKNOWN" and u.source == "fallback" and not u.customer_contact_recommended


def test_triage_uses_llm_only_for_unknown_codes() -> None:
    llm = LLMGateway({"groq": StubProvider(lambda _m: '{"category":"CARD_EXPIRED","rationale":"expiry"}', "groq")})
    out = failure_triage.triage(failure_triage.TriageIn(error_code="E", error_reason="card_validity_lapsed"), llm)
    assert out.category == "CARD_EXPIRED" and out.source == "llm"


TE = platform_default("whatsapp.recovery", "te") or ""


def _draft_in() -> conversation.DraftIn:
    return conversation.DraftIn(customer_name="Priya", language="te", amount=Money.of("999"), plan_name="Chai Club",
                                failure_category="INSUFFICIENT_FUNDS", template=TE,
                                template_ref="whatsapp.recovery@te:v0", template_language="te")


def test_template_fallback_uses_the_registry_wording_passed_in() -> None:
    custom = conversation.DraftIn(customer_name="Priya", language="te", amount=Money.of("999"), plan_name="Chai Club",
                                  failure_category="INSUFFICIENT_FUNDS",
                                  template="{name} గారు, {plan} {amount} బాకీ. {link}",
                                  template_ref="whatsapp.recovery@te:v7", template_language="te")
    out = conversation.draft(custom, None)
    assert out.text_with_placeholder == "Priya గారు, Chai Club ₹999.00 బాకీ. {link}"
    assert out.template_ref == "whatsapp.recovery@te:v7" and out.source == "template"


def test_conversation_falls_back_when_llm_breaks_hard_rules() -> None:
    inp = _draft_in()
    lying = LLMGateway({"sarvam": StubProvider(
        lambda _m: '{"text":"Pay ₹1,999 now or we will call your family {link}","language":"te"}', "sarvam")})
    out = conversation.draft(inp, lying)
    assert out.source == "template" and "₹999.00" in out.text_with_placeholder and "{link}" in out.text_with_placeholder
    assert "నమస్తే" in out.text_with_placeholder
    english_for_telugu = LLMGateway({"sarvam": StubProvider(
        lambda _m: '{"text":"Hi Priya, ₹999.00 for Chai Club is pending. Pay here: {link}","language":"te"}', "sarvam")})
    assert conversation.draft(inp, english_for_telugu).source == "template"   # wrong script → template
    good = LLMGateway({"sarvam": StubProvider(
        lambda _m: '{"text":"నమస్తే ప్రియ, చాయ్ క్లబ్ కోసం ₹999.00 చెల్లింపు బాకీ ఉంది. ఇక్కడ చెల్లించండి: {link}",'
                   '"language":"te"}', "sarvam")})
    assert conversation.draft(inp, good).source == "llm"


def test_debit_strategist_is_deterministic_and_conservative() -> None:
    si = debit_strategist.StrategyIn
    assert debit_strategist.plan(si(debit_id="d", p_fail=None, risk_threshold=0.35)).risk_band == "unknown"
    hi = debit_strategist.plan(si(debit_id="d", p_fail=0.6, risk_threshold=0.35, cash_day_distance=-5))
    assert hi.enhanced_notice and hi.propose_debit_shift and hi.send_predebit_notice
    lender = debit_strategist.plan(si(debit_id="d", p_fail=0.6, risk_threshold=0.35, cash_day_distance=-5,
                                      segment="lending"))
    assert not lender.propose_debit_shift
    # the threshold is the tenant's, not a code constant
    assert debit_strategist.plan(si(debit_id="d", p_fail=0.6, risk_threshold=0.7)).risk_band == "low"


def test_every_agent_has_a_complete_spec() -> None:
    from nirantar.mcp.tools import AGENT_SCOPES

    assert set(AGENT_SCOPES) <= set(SPECS), "every agent that can call tools has a reviewed spec"
    for s in SPECS.values():
        assert s.fallback and s.escalates_when and s.status in ("implemented", "planned")


@pytest.mark.sandbox
def test_live_telugu_draft_via_sarvam() -> None:
    if not os.environ.get("SARVAM_API_KEY"):
        pytest.skip("SARVAM_API_KEY not set")
    llm = LLMGateway.from_env()
    inp = _draft_in()
    out = conversation.draft(inp, llm)
    assert "₹999.00" in out.text_with_placeholder and out.text_with_placeholder.count("{link}") == 1
    print("source:", out.source, "| llm calls:", [(r.provider, r.model, r.ok, r.error) for r in llm.records])
