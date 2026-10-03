from __future__ import annotations

from datetime import date

from nirantar.agents import collections, mandate_doctor, treasury
from nirantar.core.money import Money

TODAY = date(2026, 10, 1)


def m(**kw: object) -> mandate_doctor.MandateIn:
    base: dict[str, object] = dict(mandate_id="m", rail="card", status="active", valid_until=date(2028, 1, 1),
                                   max_amount_minor=200_000, next_debit_amount_minor=99_900,
                                   next_debit_on=date(2026, 10, 8), today=TODAY)
    base.update(kw)
    return mandate_doctor.MandateIn(**base)  # type: ignore[arg-type]


def test_mandate_doctor_rules() -> None:
    assert mandate_doctor.diagnose(m()).repair == "none"
    assert mandate_doctor.diagnose(m(valid_until=date(2026, 10, 5))).repair == "reauth_card_expiring"
    assert mandate_doctor.diagnose(m(valid_until=date(2026, 10, 20))).repair == "reauth_card_expiring"
    assert mandate_doctor.diagnose(m(status="paused")).repair == "resume_paused"
    assert mandate_doctor.diagnose(m(status="revoked")).repair == "reauth_revoked"
    fraud = mandate_doctor.diagnose(m(status="revoked", revoked_reason="fraud"))
    assert fraud.repair == "human_review" and not fraud.customer_action_needed
    assert mandate_doctor.diagnose(m(max_amount_minor=50_000)).repair == "raise_limit"


def test_treasury_plans_on_pessimistic_inflow_and_rounds_deterministically() -> None:
    days = [treasury.DayPosition(date(2026, 10, d), Money.of("50000"), Money.of("30000"), Money.of("45000"))
            for d in range(1, 6)]
    actions, worst = treasury.plan(Money.of("40000"), days, buffer=Money.of("20000"))
    assert worst == Money.of("-35000")                       # 40k + 5*(30k-45k)
    draw = next(a for a in actions if a.kind == "request_credit_draw")
    assert draw.amount == Money.of("60000") and draw.needs_approval   # gap 55k → rounded up to 60k
    assert next(a for a in actions if a.kind == "prioritise_collections").needs_approval is False
    healthy, _ = treasury.plan(Money.of("500000"), days, buffer=Money.of("20000"))
    assert healthy == []


def test_collections_buckets_disclosure_and_hardship() -> None:
    c = collections.LoanCase("L1", 12, 500_000, agent_disclosed=False, hardship_flag=False,
                             grid_allows_restructure=True)
    p = collections.plan(c)
    assert p.bucket == "1-30" and p.steps[0] == "disclose_agent_details"
    p2 = collections.plan(collections.LoanCase("L2", 45, 500_000, True, False, True))
    assert p2.steps == ("voice_reminder", "restructure_offer")
    p3 = collections.plan(collections.LoanCase("L3", 120, 500_000, True, False, False))
    assert "legal_notice_draft" in p3.steps and p3.human_review
    hardship = collections.plan(collections.LoanCase("L4", 45, 500_000, True, True, True))
    assert hardship.steps == ("hardship_review",) and hardship.human_review
