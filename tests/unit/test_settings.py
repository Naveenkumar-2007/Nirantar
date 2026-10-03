"""Tenant settings schemas, template checks and the learning estimators (pure functions, no DB)."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from nirantar.settings import learning
from nirantar.settings.schema import Effects, Strategy, platform_defaults, validate
from nirantar.settings.templates import TemplateRejected, check, platform_default, render, specs

NS = platform_defaults()["namespaces"]


def test_platform_defaults_are_valid_for_every_namespace() -> None:
    for ns, value in NS.items():
        validate(ns, value)


@pytest.mark.parametrize(("ns", "value"), [
    ("channels", {"capacity_per_round": {"whatsapp": -1, "voice": 0}, "cost_minor": {"whatsapp": 1, "voice": 1}}),
    ("channels", {"capacity_per_round": {"whatsapp": 1}, "cost_minor": {"whatsapp": 1, "voice": 1}}),
    ("experiments", {"holdout_bp": 9000, "holdout_opt_in": False}),
    ("experiments", {"holdout_bp": 800, "holdout_opt_in": False, "surprise": 1}),
    ("strategy", {"risk_threshold": {**NS["strategy"]["risk_threshold"], "fixed": 1.5}}),
    ("effects", {**NS["effects"], "prior": {"UNKNOWN": {"whatsapp": 0.1, "voice": 0.1}}}),
])
def test_invalid_settings_are_rejected(ns: str, value: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        validate(ns, value)


def test_policy_settings_can_only_tighten_regulation() -> None:
    validate("policy", {"contact_window": ["10:00", "18:00"], "max_contacts_7d": 2})
    with pytest.raises(ValueError, match="narrowed"):
        validate("policy", {"contact_window": ["07:00", "22:00"]})
    with pytest.raises(ValueError, match="lengthened"):
        validate("policy", {"predebit_notice_hours": 12})
    with pytest.raises(ValueError, match="unknown"):
        validate("policy", {"invent_a_rule": True})


# ------------------------------------------------------------------ templates
def test_every_platform_default_template_passes_its_own_checks() -> None:
    for key in specs():
        for lang in ("en", "hi", "te"):
            body = platform_default(key, lang)
            if body is not None:
                assert check(key, lang, body)["passed"], (key, lang)


@pytest.mark.parametrize(("body", "problem"), [
    ("Hi {name}, please pay here: {link}", "missing required placeholders: ['amount']"),
    ("Hi {name}, pay ₹999 for {plan} here: {link} {amount}", "literal money amount"),
    ("Hi {name}, pay Rs 500 now {amount} {link}", "literal money amount"),
    ("Hi {name}, {amount} due or else we tell your employer. {link}",
     "not allowed in customer messages: ultimatum ('or else'), threat to involve family/employer/others"),
    ("Hi {name}, {amount} due by {deadline}: {link}", "unknown placeholders: ['deadline']"),
])
def test_bad_proposals_are_rejected_with_reasons(body: str, problem: str) -> None:
    with pytest.raises(TemplateRejected) as exc:
        check("whatsapp.recovery", "en", body)
    assert any(problem in p for p in exc.value.problems), exc.value.problems


def test_telugu_template_must_be_written_in_telugu() -> None:
    with pytest.raises(TemplateRejected, match="script"):
        check("whatsapp.recovery", "te", "Hi {name}, your {amount} payment failed. Pay here: {link}")


def test_render_never_evaluates_unknown_braces() -> None:
    assert render("{amount} {__class__} {link}", {"amount": "₹1.00"}) == "₹1.00 {__class__} {link}"


# ------------------------------------------------------------------ learning: contact effects
def _rows(cat: str, n_t: int, p_t: float, n_h: int, p_h: float, contact: float, arm: str,
          rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for _ in range(n_t):
        rows.append({"category": cat, "assigned": "treatment", "recovered": rng.random() < p_t,
                     "exposed": arm if rng.random() < contact else "none"})
    for _ in range(n_h):
        rows.append({"category": cat, "assigned": "holdout", "recovered": rng.random() < p_h, "exposed": "holdout"})
    return rows


def test_effects_are_learned_from_holdout_contrast_and_shrunk_to_prior() -> None:
    cfg = Effects.model_validate(NS["effects"])
    rng = random.Random(1)
    # True ITT = 0.18 with 90% contact → CACE = 0.2 on WhatsApp for INSUFFICIENT_FUNDS
    rows = _rows("INSUFFICIENT_FUNDS", 4000, 0.58, 4000, 0.40, 0.9, "whatsapp", rng)
    effects, ev = learning.estimate_effects(rows, cfg)
    cat = ev["categories"]["INSUFFICIENT_FUNDS"]
    assert cat["source"] == "learned" and cat["channel_split"] == "single channel"
    assert cat["cace_ci95"][0] < 0.2 < cat["cace_ci95"][1]
    assert abs(effects["INSUFFICIENT_FUNDS"]["whatsapp"] - 0.2) < 0.03       # 50 prior pseudo-obs vs ~3600 real
    assert effects["INSUFFICIENT_FUNDS"]["voice"] == cfg.prior["INSUFFICIENT_FUNDS"]["voice"]   # no voice data
    assert ev["categories"]["MANDATE_REVOKED"]["source"] == "prior"


def test_small_groups_keep_the_prior() -> None:
    cfg = Effects.model_validate(NS["effects"])
    rows = _rows("CARD_EXPIRED", 10, 0.9, 10, 0.1, 1.0, "whatsapp", random.Random(2))
    effects, ev = learning.estimate_effects(rows, cfg)
    assert effects["CARD_EXPIRED"] == cfg.prior["CARD_EXPIRED"] and ev["categories"]["CARD_EXPIRED"]["source"] == "prior"


def test_negative_estimates_are_floored_at_zero() -> None:
    cfg = Effects.model_validate({**NS["effects"], "prior_strength": 0})
    rows = _rows("UNKNOWN", 2000, 0.30, 2000, 0.45, 1.0, "whatsapp", random.Random(3))
    effects, _ = learning.estimate_effects(rows, cfg)
    assert effects["UNKNOWN"]["whatsapp"] == 0.0


# ------------------------------------------------------------------ learning: risk threshold
def test_threshold_is_cost_sensitive_and_needs_evidence() -> None:
    cfg = Strategy.model_validate(NS["strategy"])
    rng = random.Random(4)
    rows = []
    for _ in range(3000):
        s = rng.random()
        rows.append((s, rng.random() < s ** 2, 99_900))      # failures concentrate at high scores
    thr, ev = learning.estimate_threshold(rows, cfg)
    assert thr is not None and ev["value_minor_at_threshold"] >= ev["value_minor_at_fallback"]
    # expected gain per flag = s² · ₹999 · 5% − ₹1  ≥ 0  ⇔  s ≥ ~0.14
    assert 0.08 <= thr <= 0.25
    dearer = Strategy.model_validate({"risk_threshold": {**NS["strategy"]["risk_threshold"], "flag_cost_minor": 2500}})
    thr2, _ = learning.estimate_threshold(rows, dearer)
    assert thr2 is not None and thr2 > thr                  # costlier flags → flag fewer debits
    none, ev_small = learning.estimate_threshold(rows[:100], cfg)
    assert none is None and "needs" in ev_small["why"]
