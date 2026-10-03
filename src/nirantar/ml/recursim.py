"""RecurSim — controlled recurring-payment simulator with true counterfactuals (BB-§19).

What it models (all parameters in SimConfig, all randomness from one seed):
- Customers: segment, bank, payment rail, salary day (regular or irregular income),
  income, financial stress, an AR(1) monthly cash shock, engagement, language,
  preference for voice, card expiry, contact fatigue.
- Debits: monthly on the customer's billing anchor, executed at a non-peak hour.
- Failures with realistic codes and precedence:
  MANDATE_REVOKED > CARD_EXPIRED > BANK_TECHNICAL (TD) > LIMIT_EXCEEDED > INSUFFICIENT_FUNDS (BD).
- Banks: an incident process (outages of 1–12h) that drives technical declines,
  plus an hourly transaction stream per bank with ground-truth incident labels (for M4).
- Recovery within 7 days under each arm {none, whatsapp, voice}: potential outcomes
  Y(arm) share one uniform draw, so they are consistent and monotone in p(arm).
  The *true* treatment effect tau(arm, x) is exported for honest uplift evaluation.
- Churn after unrecovered failures (and from over-contact), voluntary churn.

What it is NOT: production data. Every artefact carries source="recursim".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ARMS = ("none", "whatsapp", "voice")
CODES = ("MANDATE_REVOKED", "CARD_EXPIRED", "BANK_TECHNICAL", "LIMIT_EXCEEDED", "INSUFFICIENT_FUNDS")
LANGS = np.array(["hi", "te", "en", "ta", "kn", "bn", "mr"])
WHATSAPP_LANGS = {"hi", "te", "en", "ta"}  # tenant has templates for these


@dataclass(frozen=True)
class SimConfig:
    n_customers: int = 4000
    months: int = 12
    start: date = date(2025, 10, 1)
    n_banks: int = 8
    seed: int = 7
    arm_probs: tuple[float, float, float] = (0.34, 0.33, 0.33)  # randomized (RCT) assignment on failures
    incident_rate_per_day: float = 0.08                          # per bank
    hourly_txn_rate: float = 400.0                                # per bank, for the M4 stream


@dataclass
class SimResult:
    config: SimConfig
    customers: pd.DataFrame
    debits: pd.DataFrame        # one row per scheduled debit (observed columns + `true_*` columns)
    failures: pd.DataFrame      # one row per failed debit: assigned arm, observed outcome, `cf_*`, `tau_*`
    bank_hourly: pd.DataFrame   # per bank per hour: attempts, technical failures, in_incident (truth)
    incidents: pd.DataFrame     # ground-truth outages


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _month_start(cfg: SimConfig, m: int) -> date:
    y, mo = cfg.start.year + (cfg.start.month - 1 + m) // 12, (cfg.start.month - 1 + m) % 12 + 1
    return date(y, mo, 1)


def simulate(cfg: SimConfig | None = None) -> SimResult:
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.seed)
    n = cfg.n_customers

    # ---------------------------------------------------------------- customers
    segment = rng.choice(["subscription", "lending", "sip"], size=n, p=[0.7, 0.2, 0.1])
    bank = rng.choice(cfg.n_banks, size=n, p=np.linspace(2, 1, cfg.n_banks) / np.linspace(2, 1, cfg.n_banks).sum())
    rail = rng.choice(["upi_autopay", "emandate", "card"], size=n, p=[0.55, 0.25, 0.20])
    salary_day = rng.choice([1, 5, 7, 10, 15, 25, 28], size=n, p=[0.3, 0.15, 0.15, 0.15, 0.1, 0.1, 0.05])
    irregular = rng.random(n) < 0.15
    income = rng.lognormal(mean=np.log(35000), sigma=0.6, size=n)
    stress = rng.beta(2, 5, size=n)
    engagement = rng.beta(3, 3, size=n)
    language = rng.choice(LANGS, size=n, p=[0.35, 0.2, 0.2, 0.08, 0.07, 0.05, 0.05])
    prefers_voice = rng.random(n) < 0.35
    anchor = rng.integers(1, 29, size=n)
    amount = np.where(
        segment == "subscription", rng.choice([199, 499, 999, 1499, 2999], size=n, p=[0.2, 0.3, 0.3, 0.12, 0.08]),
        np.where(segment == "lending", rng.choice([2500, 4999, 8999, 14999], size=n), rng.choice([500, 1000, 2500, 5000],
                                                                                                    size=n)),
    ).astype(np.int64) * 100  # paise
    mandate_max = np.where(rng.random(n) < 0.03, amount - 100, amount * 2)  # a few mandates are set too low
    card_expiry_month = np.where(rail == "card", rng.integers(2, 30, size=n), 10_000)
    signup_month = np.where(rng.random(n) < 0.7, 0, rng.integers(1, max(2, cfg.months // 2), size=n))

    # Observable CRM proxies (engagement / voice preference themselves are latent)
    app_opens_30d = rng.poisson(2 + 14 * engagement)
    call_answer_rate = np.clip(rng.beta(2, 3, size=n) + 0.35 * prefers_voice, 0, 1)

    customers = pd.DataFrame({
        "customer_id": [f"cus_sim{i:06d}" for i in range(n)], "segment": segment, "bank_id": bank, "rail": rail,
        "app_opens_30d": app_opens_30d, "call_answer_rate": call_answer_rate,
        "salary_day": salary_day, "irregular_income": irregular, "income_minor": (income * 100).astype(np.int64),
        "stress": stress, "engagement": engagement, "language": language, "prefers_voice": prefers_voice,
        "anchor_day": anchor, "amount_minor": amount, "mandate_max_minor": mandate_max, "signup_month": signup_month,
    })

    # ---------------------------------------------------------------- bank incidents + hourly stream (M4)
    horizon_days = (_month_start(cfg, cfg.months) - cfg.start).days
    t0 = datetime(cfg.start.year, cfg.start.month, cfg.start.day)
    inc_rows = []
    for b in range(cfg.n_banks):
        k = rng.poisson(cfg.incident_rate_per_day * horizon_days * (1.5 if b == cfg.n_banks - 1 else 1.0))
        starts = rng.uniform(0, horizon_days * 24, size=k)
        durs = rng.uniform(1, 12, size=k)
        for s, d in zip(starts, durs, strict=True):
            # half the incidents are partial degradations (2–10% extra failures), half are outages
            severity = float(rng.uniform(0.02, 0.10) if rng.random() < 0.5 else rng.uniform(0.3, 0.9))
            inc_rows.append({"bank_id": b, "start": t0 + timedelta(hours=float(s)),
                             "end": t0 + timedelta(hours=float(s + d)), "severity": severity})
    incidents = pd.DataFrame(inc_rows, columns=["bank_id", "start", "end", "severity"])

    by_bank: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for b in range(cfg.n_banks):
        sub = incidents[incidents.bank_id == b]
        by_bank[b] = (
            np.array([(s - t0).total_seconds() for s in sub.start]),
            np.array([(e - t0).total_seconds() for e in sub.end]),
            sub.severity.to_numpy(dtype=float),
        )

    def incident_severity(b: int, ts: datetime) -> float:
        starts, ends, sev = by_bank[b]
        if not len(starts):
            return 0.0
        t = (ts - t0).total_seconds()
        hit = (starts <= t) & (ends > t)
        return float(sev[hit].max()) if hit.any() else 0.0

    hours = horizon_days * 24
    hourly = []
    for b in range(cfg.n_banks):
        base_td = 0.004 + 0.002 * b / cfg.n_banks
        in_inc = np.zeros(hours)
        sev = np.zeros(hours)
        for r in incidents[incidents.bank_id == b].itertuples():
            h0 = int((r.start - t0).total_seconds() // 3600)
            h1 = int(np.ceil((r.end - t0).total_seconds() / 3600))
            in_inc[max(0, h0):min(hours, h1)] = 1
            sev[max(0, h0):min(hours, h1)] = np.maximum(sev[max(0, h0):min(hours, h1)], r.severity)
        diurnal = 0.4 + 0.6 * np.sin(np.pi * ((np.arange(hours) % 24) / 24.0)) ** 2
        attempts = rng.poisson(cfg.hourly_txn_rate * diurnal)
        fails = rng.binomial(attempts, np.clip(base_td + sev * 0.8, 0, 1))
        hourly.append(pd.DataFrame({"bank_id": b, "hour": [t0 + timedelta(hours=int(h)) for h in range(hours)],
                                    "attempts": attempts, "technical_failures": fails, "in_incident": in_inc.astype(bool)}))
    bank_hourly = pd.concat(hourly, ignore_index=True)

    # ---------------------------------------------------------------- monthly debits
    shock = rng.normal(0, 0.6, size=n)
    active = np.ones(n, dtype=bool)
    revoked = np.zeros(n, dtype=bool)
    card_renewed_at = np.full(n, -1)
    contacts_30d = np.zeros(n)
    debit_rows, failure_rows = [], []
    dcount = 0
    for m in range(cfg.months):
        ms = _month_start(cfg, m)
        dim = calendar.monthrange(ms.year, ms.month)[1]
        shock = 0.6 * shock + rng.normal(0, 0.6, size=n)            # AR(1) cash shock: history is informative
        revoked |= active & ~revoked & (rng.random(n) < 0.004 + 0.02 * stress + 0.01 * (contacts_30d > 2))
        contacts_30d = contacts_30d * 0.3                            # decays month to month
        live = active & (signup_month <= m)
        for i in np.flatnonzero(live):
            day = int(min(anchor[i], dim))
            sched = date(ms.year, ms.month, day)
            hour = int(rng.choice([0, 1, 2, 3, 4, 5, 6, 22, 23]))
            exec_ts = datetime(sched.year, sched.month, sched.day, hour, int(rng.integers(0, 60)))
            sal = int(salary_day[i]) + (int(rng.integers(-5, 6)) if irregular[i] else 0)
            sal = int(np.clip(sal, 1, dim))
            dss = (day - sal) % dim                                  # days since salary at execution
            days_to_salary = (sal - day) % dim or dim
            card_expired = rail[i] == "card" and m >= card_expiry_month[i] and card_renewed_at[i] < card_expiry_month[i]
            sev = incident_severity(int(bank[i]), exec_ts)
            p_td = 0.004 + 0.85 * sev
            logit = (-3.2 + 2.6 * stress[i] + 1.1 * np.log(amount[i] / (income[i] * 100 * 0.1))
                     + 1.5 * (dss >= 20) + 0.9 * (dss >= 25) + 1.0 * shock[i] + 0.8 * irregular[i])
            p_nsf = float(_sigmoid(np.array(logit)))
            code = None
            if revoked[i]:
                code = "MANDATE_REVOKED"
            elif card_expired:
                code = "CARD_EXPIRED"
            elif rng.random() < p_td:
                code = "BANK_TECHNICAL"
            elif amount[i] > mandate_max[i]:
                code = "LIMIT_EXCEEDED"
            elif rng.random() < p_nsf:
                code = "INSUFFICIENT_FUNDS"
            debit_id = f"dbt_sim{dcount:07d}"
            dcount += 1
            debit_rows.append({
                "debit_id": debit_id, "customer_id": customers.customer_id.iat[i], "month": m,
                "scheduled_for": sched, "executed_at": exec_ts, "amount_minor": int(amount[i]), "bank_id": int(bank[i]),
                "rail": rail[i], "succeeded": code is None, "failure_code": code,
                "true_p_insufficient": p_nsf, "true_p_technical": p_td, "true_days_since_salary": dss,
            })
            if code is None:
                continue
            # ---- potential outcomes of the 7-day recovery under each arm
            fat_hi = float(contacts_30d[i] > 2)
            lang_ok = float(language[i] in WHATSAPP_LANGS)
            if code == "BANK_TECHNICAL":
                p0, t_wa, t_vo = 0.92, 0.01, 0.01
            elif code == "INSUFFICIENT_FUNDS":
                p0 = float(np.clip(0.45 + 0.30 * (days_to_salary <= 7) - 0.35 * stress[i] - 0.15 * (shock[i] > 1), 0.02, 0.95))
                t_wa = 0.14 * engagement[i] * lang_ok - 0.06 * fat_hi
                t_vo = (0.20 if prefers_voice[i] else 0.05) + 0.06 * (1 - engagement[i]) - 0.09 * fat_hi
            elif code == "LIMIT_EXCEEDED":
                p0, t_wa, t_vo = 0.08, 0.10 * engagement[i], 0.15
            else:  # revoked / expired card: needs customer action
                p0 = 0.04
                t_wa = 0.32 * engagement[i] * lang_ok
                t_vo = 0.22 + 0.20 * prefers_voice[i]
            p = {"none": p0, "whatsapp": float(np.clip(p0 + t_wa, 0, 1)), "voice": float(np.clip(p0 + t_vo, 0, 1))}
            u = rng.random()
            cf = {a: u < p[a] for a in ARMS}
            arm = str(rng.choice(ARMS, p=list(cfg.arm_probs)))
            recovered = cf[arm]
            base_days = days_to_salary if code == "INSUFFICIENT_FUNDS" else int(rng.integers(1, 4))
            rec_days = int(min(7, max(1, base_days - (2 if arm != "none" else 0)))) if recovered else None
            if arm != "none":
                contacts_30d[i] += 1
            if recovered and code in ("MANDATE_REVOKED", "CARD_EXPIRED"):
                revoked[i] = False
                card_renewed_at[i] = m + 36
                card_expiry_month[i] = m + 36
            failure_rows.append({
                "debit_id": debit_id, "customer_id": customers.customer_id.iat[i], "failure_code": code,
                "arm": arm, "recovered": recovered, "recovery_days": rec_days,
                "recovered_amount_minor": int(amount[i]) if recovered else 0,
                "cf_none": cf["none"], "cf_whatsapp": cf["whatsapp"], "cf_voice": cf["voice"],
                "p_none": p["none"], "p_whatsapp": p["whatsapp"], "p_voice": p["voice"],
                "tau_whatsapp": p["whatsapp"] - p["none"], "tau_voice": p["voice"] - p["none"],
                "contacts_30d_before": float(contacts_30d[i] - (arm != "none")),
            })
            if not recovered:
                churn_p = 0.7 if code in ("MANDATE_REVOKED", "CARD_EXPIRED") else 0.2 + 0.4 * stress[i]
                if rng.random() < churn_p:
                    active[i] = False
        active &= rng.random(n) >= 0.01  # voluntary churn

    debits = pd.DataFrame(debit_rows)
    failures = pd.DataFrame(failure_rows)
    for df in (customers, debits, failures, bank_hourly, incidents):
        df.attrs["source"] = "recursim"
        df.attrs["seed"] = cfg.seed
    return SimResult(cfg, customers, debits, failures, bank_hourly, incidents)
