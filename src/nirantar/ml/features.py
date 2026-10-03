"""Point-in-time features for M1 (debit failure at T-3) and the uplift models.

Leakage rules (enforced by tests/ml/test_features_leakage.py):
- A debit's own outcome is only known if it was *executed* before the cutoff.
- A failure's recovery outcome is only known if its 7-day recovery window closed before the cutoff.
- Bank health uses only hourly stats that ended before the cutoff.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from nirantar.ml.cash_window import circular_distance, estimate

FEATURE_VERSION = "m1-features-v1"
LEAD_DAYS = 3
RECOVERY_WINDOW_DAYS = 7

NUMERIC = [
    "log_amount", "tenure_months", "n_prev", "fail_rate_all", "fail_rate_last3", "last_failed",
    "days_since_last_failure", "nsf_last6", "td_last6", "mandate_issue_last6", "known_recovery_rate",
    "cash_day_distance", "cash_confidence", "cash_n_obs", "bank_td_rate_24h", "bank_td_rate_7d",
]
CATEGORICAL = ["segment", "rail", "bank_id"]


def _cutoff(scheduled_for: date) -> datetime:
    return datetime(scheduled_for.year, scheduled_for.month, scheduled_for.day) - timedelta(days=LEAD_DAYS)


def _bank_rates(bank_hourly: pd.DataFrame) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    out = {}
    for b, g in bank_hourly.sort_values("hour").groupby("bank_id"):
        hours = g.hour.to_numpy(dtype="datetime64[s]")
        out[int(b)] = (hours, np.cumsum(g.attempts.to_numpy()), np.cumsum(g.technical_failures.to_numpy()))
    return out


def _window_rate(rates: tuple[np.ndarray, np.ndarray, np.ndarray], end: datetime, hours: int) -> float:
    hrs, ca, cf = rates
    end64, start64 = np.datetime64(end, "s"), np.datetime64(end - timedelta(hours=hours), "s")
    # hour buckets fully before `end`: bucket start + 1h <= end
    hi = int(np.searchsorted(hrs, end64 - np.timedelta64(3600, "s"), side="right"))
    lo = int(np.searchsorted(hrs, start64, side="left"))
    if hi <= lo:
        return 0.0
    attempts = ca[hi - 1] - (ca[lo - 1] if lo > 0 else 0)
    fails = cf[hi - 1] - (cf[lo - 1] if lo > 0 else 0)
    return float(fails / attempts) if attempts else 0.0


def build_features(customers: pd.DataFrame, debits: pd.DataFrame, failures: pd.DataFrame,
                   bank_hourly: pd.DataFrame) -> pd.DataFrame:
    """One row per debit with features as of T-3 days and the label `failed`."""
    cust = customers.set_index("customer_id")
    rec = failures.set_index("debit_id")[["recovered", "recovery_days"]] if len(failures) else None
    rates = _bank_rates(bank_hourly)
    rows = []
    for cid, g in debits.sort_values("scheduled_for").groupby("customer_id", sort=False):
        c = cust.loc[cid]
        hist = g.to_dict("records")
        for i, d in enumerate(hist):
            cutoff = _cutoff(d["scheduled_for"])
            known = [h for h in hist[:i] if h["executed_at"] < cutoff]
            outcomes = [0 if h["succeeded"] else 1 for h in known]
            codes = [h["failure_code"] for h in known[-6:]]
            last_fail = next((h for h in reversed(known) if not h["succeeded"]), None)
            cash_days, cash_w, rec_known, rec_hits = [], [], 0, 0
            for h in known:
                if h["succeeded"]:
                    cash_days.append(h["scheduled_for"].day)
                    cash_w.append(1.0)
                    continue
                window_closed = (datetime.combine(h["scheduled_for"], datetime.min.time())
                                 + timedelta(days=RECOVERY_WINDOW_DAYS)) < cutoff
                if rec is None or not window_closed or h["debit_id"] not in rec.index:
                    continue
                r = rec.loc[h["debit_id"]]
                rec_known += 1
                if bool(r.recovered):
                    rec_hits += 1
                    if h["failure_code"] == "INSUFFICIENT_FUNDS" and r.recovery_days == r.recovery_days:
                        cash_days.append((h["scheduled_for"] + timedelta(days=int(r.recovery_days))).day)
                        cash_w.append(3.0)
            cw = estimate(cash_days, cash_w)
            rows.append({
                "debit_id": d["debit_id"], "customer_id": cid, "scheduled_for": d["scheduled_for"],
                "month": d["month"], "amount_minor": d["amount_minor"],
                "log_amount": float(np.log(d["amount_minor"])),
                "tenure_months": d["month"] - int(c.signup_month),
                "n_prev": len(known),
                "fail_rate_all": float(np.mean(outcomes)) if outcomes else 0.0,
                "fail_rate_last3": float(np.mean(outcomes[-3:])) if outcomes else 0.0,
                "last_failed": float(outcomes[-1]) if outcomes else 0.0,
                "days_since_last_failure": float((cutoff - last_fail["executed_at"]).days) if last_fail else 999.0,
                "nsf_last6": sum(1 for x in codes if x == "INSUFFICIENT_FUNDS"),
                "td_last6": sum(1 for x in codes if x == "BANK_TECHNICAL"),
                "mandate_issue_last6": sum(1 for x in codes if x in ("MANDATE_REVOKED", "CARD_EXPIRED")),
                "known_recovery_rate": rec_hits / rec_known if rec_known else -1.0,
                "cash_day_distance": circular_distance(d["scheduled_for"].day, cw.day) if cw.day else 0.0,
                "cash_confidence": cw.confidence, "cash_n_obs": cw.n_obs,
                "bank_td_rate_24h": _window_rate(rates[int(d["bank_id"])], cutoff, 24),
                "bank_td_rate_7d": _window_rate(rates[int(d["bank_id"])], cutoff, 24 * 7),
                "segment": c.segment, "rail": c.rail, "bank_id": int(d["bank_id"]),
                "failed": int(not d["succeeded"]), "failure_code": d["failure_code"],
            })
    out = pd.DataFrame(rows)
    out.attrs["feature_version"] = FEATURE_VERSION
    out.attrs["source"] = debits.attrs.get("source", "unknown")
    return out
