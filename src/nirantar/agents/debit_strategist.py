"""Debit Strategist (T-3): turn M1 risk into a pre-debit plan. Deterministic.

It never picks dates or amounts: a debit shift is only *proposed* with the earliest legal time computed by
the policy engine, and it goes through the Compliance Guardian like any other action.
The risk threshold is the tenant's (learned from its verified outcomes, or configured) — see settings.learning.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StrategyIn(BaseModel):
    debit_id: str
    p_fail: float | None             # None = model unavailable
    risk_threshold: float = Field(gt=0, lt=1)
    cash_day_distance: float | None = None
    segment: str = "subscription"


class StrategyOut(BaseModel):
    risk_band: Literal["low", "high", "unknown"]
    send_predebit_notice: bool       # always True: it's mandatory
    enhanced_notice: bool            # notice mentions keeping funds available
    propose_debit_shift: bool
    rationale: str


def plan(inp: StrategyIn) -> StrategyOut:
    if inp.p_fail is None:
        return StrategyOut(risk_band="unknown", send_predebit_notice=True, enhanced_notice=False,
                           propose_debit_shift=False, rationale="M1 unavailable: mandatory notice only")
    high = inp.p_fail >= inp.risk_threshold
    shift = bool(high and inp.cash_day_distance is not None and inp.cash_day_distance < -2
                 and inp.segment not in ("lending", "mfi"))
    return StrategyOut(
        risk_band="high" if high else "low", send_predebit_notice=True, enhanced_notice=high,
        propose_debit_shift=shift,
        rationale=(f"p_fail={inp.p_fail:.2f} {'≥' if high else '<'} {inp.risk_threshold:.2f}"
                   + ("; debit falls before usual cash day → propose shift (policy-gated)" if shift else "")))
