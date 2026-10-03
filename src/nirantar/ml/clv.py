"""M15 customer lifetime value — shifted-beta-geometric (sBG) model for contractual subscriptions.

Model (Fader & Hardie, "How to Project Customer Retention", J. Interactive Marketing 2007): each subscriber has a
constant per-period churn probability θ; across subscribers θ ~ Beta(α, β). With T = number of periods paid:
    P(T = t)  = B(α+1, β+t−1) / B(α, β)          t = 1, 2, …   (churned after paying t periods)
    P(T ≥ t)  = B(α, β+t−1) / B(α, β)
Fitted per tenant by maximum likelihood on gold.subscription_lifecycle: churned subscribers contribute P(T = paid),
active or completed ones are right-censored and contribute P(T ≥ paid).

Residual value of an ACTIVE subscriber who has paid n periods (sum written out, verified by Monte Carlo in tests):
    E[discounted future payments] = Σ_{j≥1} P(T ≥ n+j | T ≥ n) · (1+d)^(−j)
CLV (forward-looking) = that × the subscriber's amount per period. d = per-period rate from the tenant's annual
discount rate (retention settings). Goodness of fit: fitted vs Kaplan–Meier retention per period.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import betaln


@dataclass(frozen=True)
class SBGFit:
    alpha: float
    beta: float
    loglik: float
    n: int
    churned: int

    def survival(self, t: int) -> float:
        """P(T ≥ t): paid at least t periods."""
        return 1.0 if t <= 1 else math.exp(betaln(self.alpha, self.beta + t - 1) - betaln(self.alpha, self.beta))

    def p_renew(self, n: int) -> float:
        """P(pays period n+1 | paid n periods) = (β+n−1)/(α+β+n−1)."""
        return (self.beta + n - 1) / (self.alpha + self.beta + n - 1)

    def residual_payments(self, n: int, d: float, tol: float = 1e-10, max_terms: int = 2400) -> float:
        """Σ_{j≥1} P(T ≥ n+j | T ≥ n)(1+d)^(−j), computed as a running product of renewal probabilities."""
        total, cond, disc = 0.0, 1.0, 1.0
        for j in range(1, max_terms + 1):
            cond *= self.p_renew(n + j - 1)
            disc /= 1.0 + d
            term = cond * disc
            total += term
            if term < tol:
                break
        return total


def fit_sbg(paid: np.ndarray, churned: np.ndarray) -> SBGFit:
    """paid: periods paid per subscriber (≥ 1); churned: True if churned (else right-censored)."""
    paid = np.asarray(paid, dtype=float)
    churned = np.asarray(churned, dtype=bool)
    if len(paid) == 0 or (paid < 1).any():
        raise ValueError("every subscriber needs ≥ 1 paid period")

    def nll(params: np.ndarray) -> float:
        a, b = np.exp(params)
        base = betaln(a, b)
        ll_churn = betaln(a + 1, b + paid[churned] - 1) - base
        ll_alive = betaln(a, b + paid[~churned] - 1) - base
        return float(-(ll_churn.sum() + ll_alive.sum()))

    best = min((minimize(nll, np.log(x0), method="Nelder-Mead", options={"xatol": 1e-8, "fatol": 1e-9,
                                                                            "maxiter": 4000})
                for x0 in ([1.0, 1.0], [0.5, 5.0], [2.0, 20.0])), key=lambda r: r.fun)
    a, b = np.exp(best.x)
    return SBGFit(float(a), float(b), float(-best.fun), len(paid), int(churned.sum()))


def kaplan_meier(paid: np.ndarray, churned: np.ndarray, max_t: int) -> list[dict[str, float]]:
    """Empirical P(T ≥ t) with right-censoring, for the goodness-of-fit check."""
    out, s = [], 1.0
    for t in range(1, max_t + 1):
        # the step t → t+1 is observed for subscribers who paid more than t, or churned right after t;
        # a censored subscriber whose window ended at t has an unknown outcome and leaves the risk set
        at_risk = int(((paid > t) | ((paid == t) & churned)).sum())
        if at_risk == 0:
            break
        events = int(((paid == t) & churned).sum())
        out.append({"t": t, "survival": s, "at_risk": at_risk})
        s *= 1 - events / at_risk
    return out


def value_subscribers(lifecycle: pd.DataFrame, *, annual_discount_rate: float,
                      min_at_risk: int = 30) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fit sBG on the tenant's subscribers; value every ACTIVE subscriber. Returns (values, fit report)."""
    lc = lifecycle[(lifecycle.paid_cycles >= 1) & lifecycle.status.isin(["active", "churned", "completed"])]
    paid = lc.paid_cycles.to_numpy(dtype=int)
    churned = (lc.status == "churned").to_numpy()
    fit = fit_sbg(paid, churned)
    km = kaplan_meier(paid, churned, int(paid.max()))
    checked = [p for p in km if p["at_risk"] >= min_at_risk]
    errors = [abs(fit.survival(int(p["t"])) - p["survival"]) for p in checked]
    report: dict[str, Any] = {
        "model": "sBG", "alpha": fit.alpha, "beta": fit.beta, "loglik": fit.loglik, "subscribers": fit.n,
        "churned": fit.churned, "mean_churn_per_period": fit.alpha / (fit.alpha + fit.beta),
        "fit_check": {"periods_checked": len(checked), "max_abs_error": max(errors) if errors else None,
                      "curve": [{**p, "fitted": fit.survival(int(p["t"]))} for p in km[:24]]},
        "annual_discount_rate": annual_discount_rate,
    }
    report["fit_ok"] = bool(errors) and max(errors) <= 0.05
    active = lifecycle[lifecycle.status == "active"]
    rows = []
    for rec in active.to_dict("records"):
        n = max(1, int(rec["paid_cycles"]))
        period = float(rec["period_days"])
        d = (1 + annual_discount_rate) ** (period / 365.0) - 1
        residual = fit.residual_payments(n, d)
        rows.append({"tenant_id": rec["tenant_id"], "entity_id": rec["entity_id"], "paid_periods": n,
                     "p_renew_next": fit.p_renew(n), "expected_residual_payments": residual,
                     "clv_minor": round(residual * int(rec["last_amount_minor"])), "period_days": period})
    return pd.DataFrame(rows, columns=["tenant_id", "entity_id", "paid_periods", "p_renew_next",
                                       "expected_residual_payments", "clv_minor", "period_days"]), report
