"""Typed schemas for per-tenant settings namespaces. Unknown keys are rejected; ranges are enforced here so a
bad value can never reach an agent."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nirantar.policy.engine import TenantPolicyConfig

DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
ARMS = ("whatsapp", "voice")
CATEGORIES = ("BANK_TECHNICAL", "INSUFFICIENT_FUNDS", "MANDATE_REVOKED", "CARD_EXPIRED", "LIMIT_EXCEEDED", "NOT_PAID",
              "UNKNOWN")
LEGACY_CATEGORIES = frozenset(CATEGORIES) - {"NOT_PAID"}     # priors saved before pay-by-link (P8.6) existed


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Channels(_Strict):
    capacity_per_round: dict[str, int]       # keys validated: exactly ARMS
    cost_minor: dict[str, int]

    @field_validator("capacity_per_round", "cost_minor")
    @classmethod
    def _complete_non_negative(cls, v: dict[str, int]) -> dict[str, int]:
        if set(v) != set(ARMS):
            raise ValueError(f"must define exactly {list(ARMS)}")
        if any(x < 0 for x in v.values()):
            raise ValueError("must be >= 0")
        return v


class RiskThreshold(_Strict):
    mode: Literal["learned", "fixed"]
    fixed: float = Field(gt=0, lt=1)
    min_labels: int = Field(ge=50)
    min_positives: int = Field(ge=10)
    flag_cost_minor: int = Field(ge=0)
    prevention_share: float = Field(gt=0, le=1)


class Strategy(_Strict):
    risk_threshold: RiskThreshold


class Effects(_Strict):
    mode: Literal["learned", "prior"]
    prior_strength: float = Field(ge=0, le=10_000)
    min_per_group: int = Field(ge=10)
    prior: dict[str, dict[str, float]]       # category → channel; keys validated below

    @field_validator("prior")
    @classmethod
    def _valid_prior(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if set(v) == LEGACY_CATEGORIES:      # saved before NOT_PAID: inherit the tenant's own most cautious prior
            v = {**v, "NOT_PAID": dict(v["UNKNOWN"])}
        if set(v) != set(CATEGORIES):
            raise ValueError(f"prior must define exactly {list(CATEGORIES)}")
        for cat, arms in v.items():
            if set(arms) != set(ARMS) or any(not 0 <= p <= 1 for p in arms.values()):
                raise ValueError(f"{cat}: probabilities for {list(ARMS)} in [0, 1]")
        return v


class Experiments(_Strict):
    holdout_bp: int = Field(ge=0, le=3000)
    holdout_opt_in: bool


class Policy(_Strict):
    """Tightenings of the platform policy. Validated by TenantPolicyConfig.tightened (can only get stricter)."""
    model_config = ConfigDict(extra="allow", frozen=True)

    def config(self) -> TenantPolicyConfig:
        return TenantPolicyConfig().tightened(dict(self.model_extra or {}))


class Offer(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    kind: Literal["reminder", "discount_pct"]
    discount_pct: int = Field(ge=0, le=90)


class Winback(_Strict):
    holdout_bp: int = Field(ge=500, le=5000)          # a win-back program without a holdout can't prove anything
    window_days: int = Field(ge=7, le=90)
    min_days_since_churn: int = Field(ge=0, le=60)
    max_days_since_churn: int = Field(ge=7, le=365)
    max_offers_per_customer_90d: int = Field(ge=1, le=3)
    daily_capacity: int = Field(ge=0, le=100_000)
    offers: list[Offer] = Field(min_length=1, max_length=6)

    @field_validator("offers")
    @classmethod
    def _unique(cls, v: list[Offer]) -> list[Offer]:
        ids = [o.id for o in v]
        if len(ids) != len(set(ids)) or "holdout" in ids:
            raise ValueError("offer ids must be unique and not 'holdout'")
        for o in v:
            if (o.kind == "reminder") != (o.discount_pct == 0):
                raise ValueError(f"{o.id}: reminders have 0% discount; discounts need > 0%")
        return v


class AtRisk(_Strict):
    min_churn_probability: float = Field(gt=0, lt=1)
    min_lift_over_average: float = Field(ge=1, le=20)


class Retention(_Strict):
    annual_discount_rate: float = Field(ge=0, le=1)
    winback: Winback
    at_risk: AtRisk


class DebitCycle(_Strict):
    notice_days: int = Field(ge=1, le=10)              # pre-debit notice lead time (RBI e-mandate: >= 24 h)
    debit_time_local: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=3, max_length=64)
    payment_wait_hours: int = Field(ge=1, le=168)      # no webhook by then → reconcile against the provider
    recovery_window_days: int = Field(ge=1, le=30)
    round_gap_days: int = Field(ge=1, le=14)
    max_contact_rounds: int = Field(ge=0, le=5)


class DisputeOps(_Strict):
    min_win_probability: float = Field(ge=0.05, le=0.95)  # below this the case goes to a human, never auto-accepted


class MandateHealth(_Strict):
    lookahead_days: int = Field(default=10, ge=1, le=60)     # check mandates of debits due within this window
    expiry_warn_days: int = Field(default=30, ge=1, le=90)   # validity ending this soon counts as a problem
    repair_wait_days: int = Field(default=5, ge=1, le=30)    # how long a repair case waits for the customer
    link_valid_days: int = Field(default=7, ge=1, le=30)     # re-authorisation link lifetime


class Operations(_Strict):
    debit_cycle: DebitCycle
    disputes: DisputeOps
    mandate_health: MandateHealth = Field(default_factory=MandateHealth)


NAMESPACES: dict[str, type[_Strict]] = {"channels": Channels, "strategy": Strategy, "effects": Effects,
                                        "experiments": Experiments, "policy": Policy, "retention": Retention,
                                        "operations": Operations}


@cache
def platform_defaults() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DEFAULTS_DIR / "platform.yaml").read_text(encoding="utf-8"))
    return data


def validate(namespace: str, value: dict[str, Any]) -> _Strict:
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown settings namespace {namespace!r}")
    model = NAMESPACES[namespace].model_validate(value)
    if isinstance(model, Policy):
        model.config()  # raises ValueError if any override loosens a platform/regulatory default
    return model
