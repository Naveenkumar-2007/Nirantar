"""sBG CLV: recovers known parameters, and the residual-value sum matches a Monte-Carlo simulation of the model."""

from __future__ import annotations

import numpy as np
import pandas as pd

from nirantar.ml.clv import SBGFit, fit_sbg, kaplan_meier, value_subscribers


def _simulate(alpha: float, beta: float, n: int, horizon: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    theta = rng.beta(alpha, beta, n)
    lifetime = rng.geometric(theta)                        # periods paid before churning (≥ 1)
    start = rng.integers(0, horizon, n)                    # staggered cohorts → realistic censoring
    observable = horizon - start
    paid = np.minimum(lifetime, np.maximum(1, observable))
    # churn is only OBSERVED once the next payment is missed inside the window (as the lifecycle table infers it)
    churned = lifetime < observable
    return paid, churned


def test_mle_recovers_known_parameters_under_censoring() -> None:
    paid, churned = _simulate(0.7, 4.0, 20_000, 24, seed=1)
    fit = fit_sbg(paid, churned)
    assert abs(fit.alpha - 0.7) / 0.7 < 0.1 and abs(fit.beta - 4.0) / 4.0 < 0.12
    km = kaplan_meier(paid, churned, 12)
    assert max(abs(fit.survival(p["t"]) - p["survival"]) for p in km) < 0.02


def test_residual_value_matches_monte_carlo() -> None:
    fit, n, d = SBGFit(0.9, 6.0, 0.0, 0, 0), 4, 0.01
    rng = np.random.default_rng(7)
    theta = rng.beta(fit.alpha, fit.beta, 2_000_000)
    life = rng.geometric(theta)                            # periods paid
    alive = life[life >= n]                                # survivors who have paid n periods
    future = alive - n                                     # further payments they will make
    mc = float(np.mean((1 - (1 + d) ** -future.astype(float)) / (1 - 1 / (1 + d)) / (1 + d)))
    assert abs(fit.residual_payments(n, d) - mc) / mc < 0.01
    # the next-renewal probability is the closed form (β+n−1)/(α+β+n−1)
    assert abs(float(np.mean(alive >= n + 1)) - fit.p_renew(n)) < 0.002


def test_values_only_active_subscribers_and_reports_fit() -> None:
    paid, churned = _simulate(0.8, 5.0, 3000, 18, seed=3)
    lc = pd.DataFrame({"tenant_id": "t", "entity_id": [f"s{i}" for i in range(len(paid))], "paid_cycles": paid,
                       "status": np.where(churned, "churned", "active"), "period_days": 30.0,
                       "last_amount_minor": 49900})
    values, rep = value_subscribers(lc, annual_discount_rate=0.12)
    assert len(values) == int((~churned).sum()) and rep["fit_ok"]
    longer = values.sort_values("paid_periods")
    # heterogeneity: subscribers who stayed longer are more loyal → higher renewal probability and value
    assert longer.p_renew_next.is_monotonic_increasing and longer.clv_minor.iloc[-1] > longer.clv_minor.iloc[0]
