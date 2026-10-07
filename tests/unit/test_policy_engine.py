"""Compliance Guardian: each rule tested with its policy_id (the test_case of the policy record)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from nirantar.policy.engine import ActionRequest, Outcome, TenantPolicyConfig, evaluate, known_policy_ids

# 10:00 IST = 04:30 UTC ; 21:30 IST = 16:00 UTC ; 07:30 IST = 02:00 UTC
DAY = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
NIGHT = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
EARLY = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
OK_CONSENT = {"whatsapp": True, "sms": True, "voice": True}


def req(**kw: object) -> ActionRequest:
    base: dict[str, object] = dict(tenant_id="ten_x", action_kind="send_whatsapp", segment="subscription",
                                   now_utc=DAY, consents=OK_CONSENT)
    base.update(kw)
    return ActionRequest(**base)  # type: ignore[arg-type]


def ids(d: object) -> set[str]:
    return {h.policy_id for h in d.hits}  # type: ignore[attr-defined]


def test_every_cited_policy_exists_in_the_kb() -> None:
    assert "IN-RBI-RBC-RECOVERY-HOURS-001" in known_policy_ids()
    assert "NIR-GOV-MAKER-CHECKER-001" in known_policy_ids()


def test_allows_a_clean_service_message_in_window() -> None:
    d = evaluate(req(message_text="Your ₹999 payment didn't go through. Pay here: https://x"))
    assert d.outcome == Outcome.ALLOW and not d.hits and d.policy_version.startswith("policy-engine")


def test_contact_window_platform_default_and_retry_after() -> None:
    d = evaluate(req(now_utc=NIGHT))
    assert d.outcome == Outcome.DENY and "NIR-GOV-CONTACT-WINDOW-001" in ids(d)
    assert d.retry_after is not None and d.retry_after.astimezone().hour >= 0
    assert d.retry_after == datetime(2026, 10, 6, 3, 30, tzinfo=UTC)  # 09:00 IST next day


def test_lending_recovery_uses_rbi_hours() -> None:
    d = evaluate(req(segment="lending", purpose="recovery", now_utc=EARLY))  # 07:30 IST
    assert d.outcome == Outcome.DENY and "IN-RBI-RBC-RECOVERY-HOURS-001" in ids(d)
    d2 = evaluate(req(segment="lending", purpose="recovery", now_utc=datetime(2026, 10, 5, 2, 45, tzinfo=UTC)))
    assert d2.outcome == Outcome.ALLOW  # 08:15 IST is inside 08:00-19:00


def test_mandatory_notice_goes_out_even_outside_window_and_without_optional_consent() -> None:
    d = evaluate(req(now_utc=NIGHT, consents={}, mandatory_kind="predebit_notice"))
    assert d.outcome == Outcome.ALLOW


def test_consent_opt_out_offer_and_voice_registration() -> None:
    assert evaluate(req(consents={})).outcome == Outcome.DENY
    assert evaluate(req(opted_out_channels=frozenset({"whatsapp"}))).outcome == Outcome.DENY
    offer = evaluate(req(contains_offer=True))
    assert offer.outcome == Outcome.DENY and "IN-TRAI-TCCCPR-DLT-001" in ids(offer)
    voice = evaluate(req(action_kind="voice_call"))
    assert "IN-TRAI-TCCCPR-VOICE-001" in ids(voice)
    assert evaluate(req(action_kind="voice_call"), TenantPolicyConfig(voice_registered=True)).allowed


@pytest.mark.parametrize("text", [
    "Pay now or we will inform your family", "Last chance! Police complaint will be filed",
    "Pay within 2 hours or else", "we will call your employer",
])
def test_conduct_screen_blocks_harassment(text: str) -> None:
    d = evaluate(req(message_text=text))
    assert d.outcome == Outcome.DENY and "IN-RBI-RBC-RECOVERY-CONDUCT-001" in ids(d)


def test_fatigue_budget_and_lending_gates() -> None:
    assert "NIR-GOV-FATIGUE-001" in ids(evaluate(req(contacts_last_7d=3)))
    gate = evaluate(req(segment="lending", purpose="recovery", environment="production"))
    assert gate.outcome == Outcome.DENY and "IN-RBI-RBC-RECOVERY-2026AMEND-001" in ids(gate)
    info = evaluate(req(segment="lending", purpose="recovery", agent_disclosure_sent=False))
    assert info.outcome == Outcome.REQUIRE_MORE_INFORMATION


def test_money_actions_need_approval_above_threshold() -> None:
    small = evaluate(req(action_kind="create_refund", amount_minor=100_000))
    big = evaluate(req(action_kind="create_refund", amount_minor=800_000))
    assert small.allowed and big.outcome == Outcome.REQUIRE_APPROVAL
    assert evaluate(req(action_kind="legal_notice")).outcome == Outcome.REQUIRE_APPROVAL


def test_debit_shift_requires_fresh_predebit_notice() -> None:
    too_soon = evaluate(req(action_kind="shift_debit", proposed_debit_at=datetime(2026, 10, 5, 20, tzinfo=UTC)))
    assert too_soon.outcome == Outcome.DENY and "IN-RBI-EMANDATE-PREDEBIT-001" in ids(too_soon)
    ok = evaluate(req(action_kind="shift_debit", proposed_debit_at=datetime(2026, 10, 7, 20, tzinfo=UTC)))
    assert ok.allowed
    lender = TenantPolicyConfig().tightened({"allow_debit_shift": False})
    assert evaluate(req(action_kind="shift_debit", segment="lending",
                        proposed_debit_at=datetime(2026, 10, 9, tzinfo=UTC)), lender).outcome == Outcome.DENY


def test_tenants_can_only_tighten() -> None:
    cfg = TenantPolicyConfig().tightened({"contact_window": ("10:00", "18:00"), "max_contacts_7d": 2})
    assert cfg.contact_window == (time(10), time(18)) and cfg.max_contacts_7d == 2
    with pytest.raises(ValueError):
        TenantPolicyConfig().tightened({"contact_window": ("07:00", "22:00")})
    with pytest.raises(ValueError):
        TenantPolicyConfig().tightened({"predebit_notice_hours": 12})


def test_customer_requested_contact_skips_only_the_fatigue_budget() -> None:
    """The customer's own promised day: no fatigue budget, but consent, opt-out, window and conduct still apply."""
    assert evaluate(req(contacts_last_7d=3, customer_requested=True)).outcome == Outcome.ALLOW
    night = evaluate(req(contacts_last_7d=3, customer_requested=True, now_utc=NIGHT))
    assert night.outcome == Outcome.DENY and "NIR-GOV-CONTACT-WINDOW-001" in ids(night)
    no_consent = evaluate(req(customer_requested=True, consents={"whatsapp": False}))
    assert "NIR-GOV-CONSENT-001" in ids(no_consent)
    opted_out = evaluate(req(customer_requested=True, opted_out_channels=frozenset({"whatsapp"})))
    assert opted_out.outcome == Outcome.DENY
    rude = evaluate(req(customer_requested=True, message_text="pay now or we will inform your family"))
    assert rude.outcome == Outcome.DENY
