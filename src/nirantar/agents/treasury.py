"""Treasury Agent: forecast → cash plan. All arithmetic is deterministic (Money); every money movement it
proposes (payout reschedule, credit draw) is an ALWAYS-approval tool (NIR-GOV-MAKER-CHECKER-001)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nirantar.core.money import Money

ROUND_TO = Money.of("10000")


@dataclass(frozen=True)
class DayPosition:
    day: date
    expected_inflow: Money
    inflow_low: Money          # lower edge of the conformal interval (M10)
    obligations: Money


@dataclass(frozen=True)
class TreasuryAction:
    kind: str                  # prioritise_collections | reschedule_payout | request_credit_draw
    amount: Money | None
    day: date
    needs_approval: bool
    rationale: str


def plan(opening_balance: Money, days: list[DayPosition], buffer: Money) -> tuple[list[TreasuryAction], Money]:
    """Plan against the *pessimistic* inflow (interval low). Returns (actions, worst projected balance)."""
    bal, worst, worst_day = opening_balance, opening_balance, None
    for d in days:
        bal = bal + d.inflow_low - d.obligations
        if bal < worst:
            worst, worst_day = bal, d.day
    actions: list[TreasuryAction] = []
    gap = buffer - worst
    if worst_day is None or not gap.is_positive:
        return actions, worst
    actions.append(TreasuryAction("prioritise_collections", None, worst_day, False,
                                  f"projected low {worst} on {worst_day}; route voice capacity to high-value at-risk "
                                  "debits before that day"))
    # round the shortfall up to the nearest ₹10,000 — computed, never generated
    units = -(-gap.minor // ROUND_TO.minor)
    draw = ROUND_TO.times(units)
    actions.append(TreasuryAction("request_credit_draw", draw, worst_day, True,
                                  f"cover shortfall {gap} below buffer {buffer}; rounded to {draw}"))
    return actions, worst
