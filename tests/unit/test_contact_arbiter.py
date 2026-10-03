from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from nirantar.agents.contact_arbiter import Candidate, EffectPriors
from nirantar.agents.contact_arbiter import arbitrate as _arbitrate
from nirantar.settings.schema import platform_defaults

BOTH = frozenset({"whatsapp", "voice"})
_ns = platform_defaults()["namespaces"]
PRIORS = EffectPriors(_ns["effects"]["prior"], _ns["channels"]["cost_minor"])


def arbitrate(cands: list[Candidate], capacity: dict[str, int]) -> list:  # type: ignore[type-arg]
    return _arbitrate(cands, capacity, PRIORS)


def test_values_follow_the_tenant_effects_and_costs_not_code_constants() -> None:
    c = [Candidate("c1", "u1", 100_000, "INSUFFICIENT_FUNDS", frozenset({"whatsapp"}))]
    cheap = EffectPriors({**_ns["effects"]["prior"], "INSUFFICIENT_FUNDS": {"whatsapp": 0.2, "voice": 0.0}},
                         {"whatsapp": 100, "voice": 800})
    dear = EffectPriors(cheap.effects, {"whatsapp": 50_000, "voice": 800})      # contact costs more than it earns
    assert _arbitrate(c, {"whatsapp": 1, "voice": 0}, cheap)[0].expected_value_minor == 20_000 - 100
    assert _arbitrate(c, {"whatsapp": 1, "voice": 0}, dear)[0].arm is None


def test_capacity_goes_to_highest_value_and_never_to_holdout_or_technical() -> None:
    cands = [
        Candidate("c1", "u1", 1_500_000, "INSUFFICIENT_FUNDS", BOTH),   # ₹15,000
        Candidate("c2", "u2", 99_900, "INSUFFICIENT_FUNDS", BOTH),      # ₹999
        Candidate("c3", "u3", 2_000_000, "MANDATE_REVOKED", BOTH, in_holdout=True),
        Candidate("c4", "u4", 5_000_000, "BANK_TECHNICAL", BOTH),       # retry fixes it: no contact
    ]
    out = {a.case_id: a for a in arbitrate(cands, {"voice": 1, "whatsapp": 10})}
    assert out["c1"].arm == "voice"
    assert out["c2"].arm == "whatsapp"
    assert out["c3"].arm is None and "holdout" in out["c3"].reason
    assert out["c4"].arm is None


def test_respects_compliance_eligibility_and_one_contact_per_customer() -> None:
    cands = [
        Candidate("c1", "same", 1_000_000, "MANDATE_REVOKED", frozenset({"whatsapp"})),  # voice denied
        Candidate("c2", "same", 900_000, "MANDATE_REVOKED", BOTH),
    ]
    out = arbitrate(cands, {"voice": 5, "whatsapp": 5})
    assert sum(a.arm is not None for a in out) == 1
    assert all(a.arm != "voice" for a in out if a.case_id == "c1")


def test_deterministic() -> None:
    cands = [Candidate(f"c{i}", f"u{i}", 100_000 + i * 997, "INSUFFICIENT_FUNDS", BOTH) for i in range(40)]
    assert arbitrate(cands, {"voice": 7, "whatsapp": 20}) == arbitrate(cands, {"voice": 7, "whatsapp": 20})


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.integers(1_000, 5_000_000), st.sampled_from(
    ["INSUFFICIENT_FUNDS", "MANDATE_REVOKED", "BANK_TECHNICAL", "CARD_EXPIRED"]),
    st.sets(st.sampled_from(["whatsapp", "voice"])), st.booleans()), min_size=1, max_size=25),
    st.integers(0, 5), st.integers(0, 10))
def test_invariants_hold_for_any_input(rows: list[tuple[int, str, set[str], bool]], voice: int, wa: int) -> None:
    cands = [Candidate(f"c{i}", f"u{i % 7}", amt, code, frozenset(arms), hold)
             for i, (amt, code, arms, hold) in enumerate(rows)]
    out = arbitrate(cands, {"voice": voice, "whatsapp": wa})
    by_case = {c.case_id: c for c in cands}
    assert sum(a.arm == "voice" for a in out) <= voice
    assert sum(a.arm == "whatsapp" for a in out) <= wa
    per_customer: dict[str, int] = {}
    for a in out:
        c = by_case[a.case_id]
        if a.arm:
            assert a.arm in c.allowed_arms and not c.in_holdout and a.expected_value_minor > 0
            per_customer[a.customer_id] = per_customer.get(a.customer_id, 0) + 1
    assert all(n <= 1 for n in per_customer.values())
