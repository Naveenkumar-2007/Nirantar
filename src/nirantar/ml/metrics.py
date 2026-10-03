"""Evaluation metrics (BB-§39). Pure functions over numpy arrays."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


def classification_report(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict[str, float]:
    return {
        "auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "ece": expected_calibration_error(y, p, bins),
        "base_rate": float(np.mean(y)),
    }


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            ece += mask.mean() * abs(float(y[mask].mean()) - float(p[mask].mean()))
    return float(ece)


def qini_auc(y: np.ndarray, treated: np.ndarray, uplift_score: np.ndarray) -> float:
    """Normalised area between the Qini curve and the random-targeting line (RCT data).

    Sort by predicted uplift; at each cut k, qini(k) = Y_t(k) - Y_c(k) * N_t(k)/N_c(k).
    Returned value is the mean gap to the straight line from 0 to qini(N), so 0 = random.
    """
    order = np.argsort(-uplift_score, kind="stable")
    y, t = y[order].astype(float), treated[order].astype(bool)
    cum_yt = np.cumsum(y * t)
    cum_yc = np.cumsum(y * ~t)
    nt = np.cumsum(t)
    nc = np.cumsum(~t)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = cum_yt - np.where(nc > 0, cum_yc * nt / nc, 0.0)
    n = len(q)
    line = np.linspace(0, q[-1], n)
    return float(np.mean(q - line) / max(1, n))


def policy_value_true(p_by_arm: dict[str, np.ndarray], chosen: np.ndarray) -> float:
    """Expected recovery rate if we follow `chosen` arms, using TRUE simulator probabilities."""
    arms = np.array(list(p_by_arm))
    probs = np.stack([p_by_arm[a] for a in arms], axis=1)
    col = np.array([int(np.flatnonzero(arms == c)[0]) for c in chosen])
    return float(probs[np.arange(len(chosen)), col].mean())


def pehe(true_tau: np.ndarray, pred_tau: np.ndarray) -> float:
    """Precision in estimating heterogeneous effects (RMSE of tau)."""
    return float(np.sqrt(np.mean((true_tau - pred_tau) ** 2)))


def wape(actual: np.ndarray, forecast: np.ndarray) -> float:
    denom = np.abs(actual).sum()
    return float(np.abs(actual - forecast).sum() / denom) if denom else 0.0
