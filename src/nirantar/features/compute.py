"""The ONE feature transformation, used offline (training sets) and online (serving). No other code computes
model features — that is what makes training/serving skew impossible by construction (parity test in tests/).

Leakage is prevented inside this function, not by callers: given any history, it only uses what was knowable
at `as_of` — a cycle's first-attempt outcome only if that attempt happened before `as_of`; its recovery only if
it was paid before `as_of` or its recovery horizon had closed by then.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from nirantar.features.definitions import CATEGORICAL, COLD_START_RATE, NUMERIC, RECOVERY_HORIZON, SMOOTHING_K
from nirantar.ml.cash_window import circular_distance, estimate

FUNDS = {"INSUFFICIENT_FUNDS"}
TECHNICAL = {"BANK_TECHNICAL"}
MANDATE = {"MANDATE_REVOKED", "CARD_EXPIRED", "LIMIT_EXCEEDED"}


@dataclass(frozen=True)
class Cycle:
    """The fields of a gold.charge_outcomes row that features may use."""
    first_attempt_at: datetime | None
    first_attempt_failed: bool | None
    failure_category: str | None
    amount_minor: int | None
    paid_at: datetime | None
    recovered: bool
    days_to_recover: float | None
    method: str | None = None
    bank: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"a": _iso(self.first_attempt_at), "f": self.first_attempt_failed, "c": self.failure_category,
                "m": self.amount_minor, "p": _iso(self.paid_at), "r": self.recovered, "d": self.days_to_recover,
                "me": self.method, "b": self.bank}

    @classmethod
    def from_json(cls, j: dict[str, Any]) -> Cycle:
        return cls(_dt(j["a"]), j["f"], j["c"], j["m"], _dt(j["p"]), bool(j["r"]), j["d"], j.get("me"), j.get("b"))


@dataclass
class TenantEvents:
    """Tenant-wide first-attempt events, sorted by time: for base rates and bank health windows."""
    times: list[float] = field(default_factory=list)          # epoch seconds
    failed: list[bool] = field(default_factory=list)
    technical: list[bool] = field(default_factory=list)
    bank: list[str] = field(default_factory=list)

    @classmethod
    def from_cycles(cls, cycles: Sequence[Cycle]) -> TenantEvents:
        ev = sorted((c.first_attempt_at.timestamp(), bool(c.first_attempt_failed),
                     bool(c.first_attempt_failed and c.failure_category in TECHNICAL), c.bank or "unknown")
                    for c in cycles if c.first_attempt_at is not None and c.first_attempt_failed is not None)
        out = cls()
        for t, f, te, b in ev:
            out.times.append(t)
            out.failed.append(f)
            out.technical.append(te)
            out.bank.append(b)
        return out

    def _index(self) -> dict[str | None, tuple[list[float], list[int], list[int]]]:
        """Prefix sums (overall and per bank) so each window query is O(log n)."""
        idx = getattr(self, "_idx", None)
        if idx is None:
            idx = {}
            for key in [None, *sorted(set(self.bank))]:
                ts, cf, ct = [], [0], [0]
                for t, f, x, b in zip(self.times, self.failed, self.technical, self.bank, strict=True):
                    if key is None or b == key:
                        ts.append(t)
                        cf.append(cf[-1] + f)
                        ct.append(ct[-1] + x)
                idx[key] = (ts, cf, ct)
            object.__setattr__(self, "_idx", idx)
        return idx

    def window(self, as_of: datetime, span: timedelta, bank: str | None = None) -> tuple[int, int, int]:
        """(attempts, failures, technical failures) strictly before as_of within span."""
        series = self._index().get(bank)
        if series is None:
            return 0, 0, 0
        ts, cf, ct = series
        hi = bisect.bisect_left(ts, as_of.timestamp())
        lo = bisect.bisect_left(ts, (as_of - span).timestamp())
        return hi - lo, cf[hi] - cf[lo], ct[hi] - ct[lo]

    def to_json(self) -> dict[str, Any]:
        return {"t": self.times, "f": self.failed, "x": self.technical, "b": self.bank}

    @classmethod
    def from_json(cls, j: dict[str, Any]) -> TenantEvents:
        return cls(list(j["t"]), list(j["f"]), list(j["x"]), list(j["b"]))


def _iso(v: datetime | None) -> str | None:
    return None if v is None else v.isoformat()


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def compute_features(history: Sequence[Cycle], *, as_of: datetime, due_at: datetime, amount_minor: int,
                     method: str | None, bank: str | None, tenant: TenantEvents) -> dict[str, Any]:
    known = sorted((c for c in history if c.first_attempt_at is not None and c.first_attempt_at < as_of
                    and c.first_attempt_failed is not None), key=lambda c: c.first_attempt_at or as_of)
    outcomes = [bool(c.first_attempt_failed) for c in known]
    fails = [c for c in known if c.first_attempt_failed]
    last6 = known[-6:]

    def settled(c: Cycle) -> bool:     # recovery outcome knowable at as_of?
        assert c.first_attempt_at is not None
        return (c.paid_at is not None and c.paid_at < as_of) or c.first_attempt_at + RECOVERY_HORIZON <= as_of

    settled_fails = [c for c in fails if settled(c)]
    recovered = [c for c in settled_fails if c.recovered and c.paid_at is not None and c.paid_at < as_of]
    streak = 0
    for o in reversed(outcomes):
        if not o:
            break
        streak += 1
    n30, f30, _ = tenant.window(as_of, timedelta(days=30))
    base = _rate(f30, n30) if n30 else COLD_START_RATE
    n24, _, t24 = tenant.window(as_of, timedelta(hours=24))
    n7, _, t7 = tenant.window(as_of, timedelta(days=7))
    b = bank or "unknown"
    nb24, _, tb24 = tenant.window(as_of, timedelta(hours=24), b)
    nb7, _, tb7 = tenant.window(as_of, timedelta(days=7), b)
    # M2 convention: a day on which a failed debit was recovered is strong evidence money arrived (weight 3)
    paid = [c for c in known if c.paid_at is not None and c.paid_at < as_of]
    cw = estimate([c.paid_at.day for c in paid if c.paid_at], [3.0 if c.recovered else 1.0 for c in paid])
    last_amount = next((c.amount_minor for c in reversed(known) if c.amount_minor), None)
    month_end = (due_at.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    row: dict[str, Any] = {
        "log_amount": math.log(max(1, amount_minor)),
        "amount_change_ratio": (amount_minor / last_amount - 1) if last_amount else 0.0,
        "tenure_days": (as_of - known[0].first_attempt_at).days if known and known[0].first_attempt_at else 0,
        "n_prev": len(known),
        "fail_rate_all": _rate(sum(outcomes), len(outcomes)),
        "fail_rate_last3": _rate(sum(outcomes[-3:]), len(outcomes[-3:])),
        "smoothed_fail_rate": (sum(outcomes) + SMOOTHING_K * base) / (len(outcomes) + SMOOTHING_K),
        "last_failed": float(outcomes[-1]) if outcomes else 0.0,
        "consecutive_failures": streak,
        "days_since_last_failure": (as_of - fails[-1].first_attempt_at).days
        if fails and fails[-1].first_attempt_at else 999,
        "fails_funds_last6": sum(1 for c in last6 if c.first_attempt_failed and c.failure_category in FUNDS),
        "fails_technical_last6": sum(1 for c in last6 if c.first_attempt_failed and c.failure_category in TECHNICAL),
        "fails_mandate_last6": sum(1 for c in last6 if c.first_attempt_failed and c.failure_category in MANDATE),
        "known_recovery_rate": _rate(len(recovered), len(settled_fails)) if settled_fails else -1.0,
        "mean_days_to_recover": (sum(c.days_to_recover or 0.0 for c in recovered) / len(recovered))
        if recovered else -1.0,
        "due_day": due_at.day, "due_weekday": due_at.weekday(),
        "days_to_month_end": (month_end.date() - due_at.date()).days,
        "paid_day_distance": circular_distance(due_at.day, cw.day) if cw.day else 0.0,
        "paid_day_confidence": cw.confidence,
        "tenant_fail_rate_30d": base,
        "tenant_technical_rate_24h": _rate(t24, n24), "tenant_technical_rate_7d": _rate(t7, n7),
        "bank_technical_rate_24h": _rate(tb24, nb24), "bank_technical_rate_7d": _rate(tb7, nb7),
        "method": method or "unknown", "bank": b,
    }
    assert set(row) == set(NUMERIC + CATEGORICAL), "feature definitions and computation diverged"
    return row


def tenure_stats(history: Sequence[Cycle], *, as_of: datetime, default_period_days: float = 30.0) -> dict[str, float]:
    """Inputs of the tenure (sBG) churn prior — shared by training sets and serving, like compute_features:
    paid_periods = cycles paid before as_of; period_days = median gap between known first attempts."""
    known = sorted(c.first_attempt_at for c in history if c.first_attempt_at is not None and c.first_attempt_at < as_of)
    gaps = sorted((b - a).total_seconds() / 86400 for a, b in pairwise(known))
    period = gaps[len(gaps) // 2] if gaps else default_period_days
    paid = sum(1 for c in history if c.paid_at is not None and c.paid_at < as_of)
    return {"paid_periods": float(paid), "period_days": float(min(max(period, 6.0), 366.0))}
