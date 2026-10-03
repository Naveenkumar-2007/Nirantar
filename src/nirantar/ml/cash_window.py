"""M2 Cash-Window Estimator: when in the month does this customer usually have money?

Observations are days-of-month with weights:
- a recovered insufficient-funds failure paid on day D is strong evidence money arrived by D (weight 3)
- a debit that succeeded on day D is weak evidence (weight 1)
- a debit that failed for insufficient funds on day D is evidence *against* D (handled by the
  predictor as a separate feature, not here)

Days are points on a circle, so we use a weighted circular mean. The resultant
length R in [0, 1] is the confidence: 1 = always the same day, ~0 = no pattern.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

PERIOD = 30.0


@dataclass(frozen=True)
class CashWindow:
    day: float | None         # estimated day-of-month money is available (1..30), None if no evidence
    confidence: float         # resultant length R
    n_obs: int


def estimate(days: Sequence[float], weights: Sequence[float]) -> CashWindow:
    if len(days) != len(weights):
        raise ValueError("days and weights must align")
    total = sum(weights)
    if not days or total <= 0:
        return CashWindow(None, 0.0, 0)
    s = sum(w * math.sin(2 * math.pi * (d - 1) / PERIOD) for d, w in zip(days, weights, strict=True))
    c = sum(w * math.cos(2 * math.pi * (d - 1) / PERIOD) for d, w in zip(days, weights, strict=True))
    r = math.hypot(s, c) / total
    angle = math.atan2(s, c) % (2 * math.pi)
    return CashWindow(1 + angle * PERIOD / (2 * math.pi), r, len(days))


def circular_distance(a: float, b: float) -> float:
    """Signed days from b to a in (-15, 15]."""
    d = (a - b) % PERIOD
    return d - PERIOD if d > PERIOD / 2 else d
