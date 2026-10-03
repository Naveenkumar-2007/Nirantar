"""M4 Bank/Gateway Health: detect technical-decline incidents from an hourly stream.

Input per bank per hour: attempts, technical_failures. Output: alarm flag per hour.
Baseline : EWMA of the failure rate + binomial z-score.
Advanced : Bayesian Online Changepoint Detection (Adams & MacKay 2007) with a
           Beta-Binomial model; alarm when the posterior says a new regime started
           recently AND the new regime's rate is materially above the long-run rate.
Truth    : RecurSim incident intervals (source=recursim).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import betaln, gammaln


def ewma_alarms(attempts: np.ndarray, fails: np.ndarray, alpha: float = 0.02, z_threshold: float = 4.0) -> np.ndarray:
    alarms = np.zeros(len(attempts), dtype=bool)
    mean = 0.005
    for t, (n, k) in enumerate(zip(attempts, fails, strict=True)):
        if n == 0:
            continue
        rate = k / n
        z = (rate - mean) / np.sqrt(max(mean * (1 - mean), 1e-6) / n)
        alarms[t] = z > z_threshold
        if not alarms[t]:  # don't learn the incident as "normal"
            mean = (1 - alpha) * mean + alpha * rate
    return alarms


def _log_betabinom(k: np.ndarray, n: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (gammaln(n + 1) - gammaln(k + 1) - gammaln(n - k + 1)
            + betaln(k + a, n - k + b) - betaln(a, b))


def bocpd_alarms(attempts: np.ndarray, fails: np.ndarray, hazard: float = 1 / 300, a0: float = 1.0,
                 b0: float = 199.0, max_run: int = 400, recent: int = 3, rate_factor: float = 4.0,
                 cp_threshold: float = 0.5) -> np.ndarray:
    """Alarm at t if P(run length <= `recent`) > cp_threshold and the recent regime's
    posterior mean failure rate exceeds `rate_factor` x the long-run mean."""
    T = len(attempts)
    alarms = np.zeros(T, dtype=bool)
    log_r = np.array([0.0])                      # log P(run length = 0)
    a = np.array([a0])
    b = np.array([b0])
    long_run = a0 / (a0 + b0)
    lh, l1h = np.log(hazard), np.log1p(-hazard)
    for t in range(T):
        n, k = float(attempts[t]), float(fails[t])
        if n > 0:
            pred = _log_betabinom(np.full_like(a, k), np.full_like(a, n), a, b)
            growth = log_r + pred + l1h
            cp = np.logaddexp.reduce(log_r + pred + lh)
            log_r = np.concatenate([[cp], growth])
            log_r -= np.logaddexp.reduce(log_r)
            a = np.concatenate([[a0], a + k])
            b = np.concatenate([[b0], b + (n - k)])
            if len(log_r) > max_run:  # truncate the tail, renormalise
                log_r, a, b = log_r[:max_run], a[:max_run], b[:max_run]
                log_r -= np.logaddexp.reduce(log_r)
        r = np.exp(log_r)
        p_recent = r[: recent + 1].sum()
        recent_rate = float((r[: recent + 1] * (a[: recent + 1] / (a[: recent + 1] + b[: recent + 1]))).sum()
                            / max(p_recent, 1e-12))
        alarms[t] = p_recent > cp_threshold and recent_rate > rate_factor * long_run and k / max(n, 1) > 2 * long_run
        if not alarms[t] and n > 0:
            long_run = 0.99 * long_run + 0.01 * (k / n)
    return alarms


@dataclass(frozen=True)
class DetectionReport:
    incidents: int
    detected: int
    incident_recall: float
    median_detection_delay_hours: float
    false_alarms_per_bank_week: float


def evaluate(bank_hourly: pd.DataFrame, alarm_col: str) -> DetectionReport:
    detected, delays, false_episodes, incidents = 0, [], 0, 0
    weeks = 0.0
    for _, g in bank_hourly.sort_values("hour").groupby("bank_id"):
        inc = g.in_incident.to_numpy()
        alarm = g[alarm_col].to_numpy()
        weeks += len(g) / (24 * 7)
        # incidents = contiguous runs of in_incident
        t = 0
        while t < len(inc):
            if inc[t]:
                start = t
                while t < len(inc) and inc[t]:
                    t += 1
                incidents += 1
                hits = np.flatnonzero(alarm[start:t])
                if len(hits):
                    detected += 1
                    delays.append(float(hits[0]))
            else:
                t += 1
        # false alarm episodes: alarm runs that don't touch an incident (±1h grace)
        near = inc | np.roll(inc, 1) | np.roll(inc, -1)
        prev = False
        for x, n_ in zip(alarm, near, strict=True):
            if x and not n_ and not prev:
                false_episodes += 1
            prev = bool(x and not n_)
    return DetectionReport(incidents, detected, detected / incidents if incidents else 1.0,
                           float(np.median(delays)) if delays else float("inf"),
                           false_episodes / weeks if weeks else 0.0)


def run(bank_hourly: pd.DataFrame) -> tuple[DetectionReport, DetectionReport]:
    df = bank_hourly.sort_values(["bank_id", "hour"]).copy()
    df["alarm_ewma"] = False
    df["alarm_bocpd"] = False
    for _, g in df.groupby("bank_id"):
        idx = g.index
        df.loc[idx, "alarm_ewma"] = ewma_alarms(g.attempts.to_numpy(), g.technical_failures.to_numpy())
        df.loc[idx, "alarm_bocpd"] = bocpd_alarms(g.attempts.to_numpy(), g.technical_failures.to_numpy())
    return evaluate(df, "alarm_ewma"), evaluate(df, "alarm_bocpd")
