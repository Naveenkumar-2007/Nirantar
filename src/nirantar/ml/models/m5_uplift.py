"""M5 Multi-treatment uplift: which intervention (none / whatsapp / voice) changes the outcome?

Unit      : a failed debit. Outcome: recovered within 7 days.
Treatments: randomized in RecurSim (known propensities), as they will be in the live holdout design.
Features  : only observables — failure code, amount, segment, language, CRM proxies (app opens,
            call answer rate), contact fatigue, cash-window distance. Latent traits are never used.
Baseline  : T-learner (one LightGBM classifier per arm).
Advanced  : DR-learner (doubly robust pseudo-outcomes, 2-fold cross-fitting, LightGBM regressor).
Decision  : pick the arm maximising amount * tau(arm) - cost(arm); "none" if nothing beats zero.
Evaluation: uses the simulator's TRUE probabilities — PEHE and expected net value of the policy
            vs always-none / always-whatsapp / always-voice.
"""

from __future__ import annotations

from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd

from nirantar.ml.metrics import pehe, qini_auc

ARMS = ("none", "whatsapp", "voice")
TREATED_ARMS = ("whatsapp", "voice")
COST_MINOR = {"none": 0, "whatsapp": 100, "voice": 800}  # assumed ₹1 per WhatsApp, ₹8 per call (configurable)
FEATURES = ["failure_code", "log_amount", "segment", "language", "app_opens_30d", "call_answer_rate",
            "contacts_30d_before", "rail"]
CATEGORICAL = ["failure_code", "segment", "language", "rail"]


def uplift_frame(sim_customers: pd.DataFrame, sim_debits: pd.DataFrame, failures: pd.DataFrame) -> pd.DataFrame:
    d = failures.merge(sim_debits[["debit_id", "month", "amount_minor"]], on="debit_id") \
        .merge(sim_customers[["customer_id", "segment", "language", "app_opens_30d", "call_answer_rate", "rail"]],
               on="customer_id")
    d["log_amount"] = np.log(d.amount_minor)
    return d


def _x(df: pd.DataFrame, cats: dict[str, list[str]]) -> pd.DataFrame:
    x = df[FEATURES].copy()
    for c in CATEGORICAL:
        x[c] = pd.Categorical(x[c].astype(str), categories=cats[c])
    return x


def _clf() -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=50, verbose=-1,
                              random_state=7)


class TLearner:
    def fit(self, df: pd.DataFrame) -> TLearner:
        self.cats_ = {c: sorted(df[c].astype(str).unique()) for c in CATEGORICAL}
        self.models_ = {a: _clf().fit(_x(df[df.arm == a], self.cats_), df[df.arm == a].recovered.astype(int))
                        for a in ARMS}
        return self

    def mu(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        x = _x(df, self.cats_)
        return {a: self.models_[a].predict_proba(x)[:, 1] for a in ARMS}

    def tau(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        mu = self.mu(df)
        return {a: mu[a] - mu["none"] for a in TREATED_ARMS}


class DRLearner:
    def __init__(self, propensity: dict[str, float]) -> None:
        self.propensity = propensity

    def fit(self, df: pd.DataFrame, seed: int = 7) -> DRLearner:
        self.cats_ = {c: sorted(df[c].astype(str).unique()) for c in CATEGORICAL}
        rng = np.random.default_rng(seed)
        fold = rng.integers(0, 2, size=len(df))
        pseudo = {a: np.zeros(len(df)) for a in TREATED_ARMS}
        for k in (0, 1):  # cross-fitting: nuisance models never see the rows they score
            train, score = df[fold != k], df[fold == k]
            mu = TLearner().fit(train).mu(score)
            y = score.recovered.astype(float).to_numpy()
            t = score.arm.to_numpy()
            for a in TREATED_ARMS:
                pseudo[a][fold == k] = (
                    mu[a] - mu["none"]
                    + (t == a) * (y - mu[a]) / self.propensity[a]
                    - (t == "none") * (y - mu["none"]) / self.propensity["none"]
                )
        x = _x(df, self.cats_)
        self.models_ = {
            a: lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=80,
                                 verbose=-1, random_state=7).fit(x, pseudo[a])
            for a in TREATED_ARMS
        }
        return self

    def tau(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        x = _x(df, self.cats_)
        return {a: self.models_[a].predict(x) for a in TREATED_ARMS}


def choose_arms(df: pd.DataFrame, tau: dict[str, np.ndarray]) -> np.ndarray:
    amount = df.amount_minor.to_numpy(dtype=float)
    value = {"none": np.zeros(len(df))}
    for a in TREATED_ARMS:
        value[a] = amount * tau[a] - COST_MINOR[a]
    stacked = np.stack([value[a] for a in ARMS], axis=1)
    return np.array(ARMS)[stacked.argmax(axis=1)]


def true_net_value(df: pd.DataFrame, arms: np.ndarray) -> float:
    """Expected ₹ recovered minus contact cost per failure, using TRUE simulator probabilities."""
    p = np.stack([df[f"p_{a}"].to_numpy() for a in ARMS], axis=1)
    idx = np.array([ARMS.index(a) for a in arms])
    chosen_p = p[np.arange(len(df)), idx]
    cost = np.array([COST_MINOR[a] for a in arms])
    return float((df.amount_minor.to_numpy() * chosen_p - cost).mean())


def capacity_policy(df: pd.DataFrame, voice_score: np.ndarray, tau_whatsapp: np.ndarray,
                    voice_capacity: float) -> np.ndarray:
    """Voice slots are scarce (telephony concurrency, staff, fatigue budget). Give them to the
    top `voice_capacity` share by score; the rest get WhatsApp if its value beats its cost."""
    k = round(voice_capacity * len(df))
    arms = np.where(df.amount_minor.to_numpy() * tau_whatsapp > COST_MINOR["whatsapp"], "whatsapp", "none")
    if k > 0:
        top = np.argsort(-voice_score, kind="stable")[:k]
        arms[top] = "voice"
    return arms


def evaluate_capacity(train_df: pd.DataFrame, test_df: pd.DataFrame, propensity: dict[str, float],
                      voice_capacity: float = 0.2, seed: int = 7) -> dict[str, float]:
    """Net value per failure (TRUE probabilities) when only `voice_capacity` of failures can get a call."""
    amount = test_df.amount_minor.to_numpy(dtype=float)
    t_tau = TLearner().fit(train_df).tau(test_df)
    dr_tau = DRLearner(propensity).fit(train_df).tau(test_df)
    true_wa = test_df.tau_whatsapp.to_numpy()
    rng = np.random.default_rng(seed)
    scores = {
        "random": rng.random(len(test_df)),
        "largest_amount": amount,
        "t_learner": amount * t_tau["voice"],
        "dr_learner": amount * dr_tau["voice"],
        "oracle": amount * test_df.tau_voice.to_numpy(),
    }
    wa_for = {"t_learner": t_tau["whatsapp"], "dr_learner": dr_tau["whatsapp"], "oracle": true_wa}
    return {name: true_net_value(test_df, capacity_policy(test_df, s, wa_for.get(name, t_tau["whatsapp"]),
                                                          voice_capacity))
            for name, s in scores.items()}


@dataclass(frozen=True)
class UpliftReport:
    baseline: dict[str, float]
    advanced: dict[str, float]
    policy_values_minor: dict[str, float]
    arm_mix_advanced: dict[str, float]


def evaluate(train_df: pd.DataFrame, test_df: pd.DataFrame, propensity: dict[str, float]) -> UpliftReport:
    t_learner = TLearner().fit(train_df)
    dr = DRLearner(propensity).fit(train_df)
    out: dict[str, dict[str, float]] = {}
    policies: dict[str, float] = {f"always_{a}": true_net_value(test_df, np.array([a] * len(test_df))) for a in ARMS}
    for name, model in (("baseline", t_learner), ("advanced", dr)):
        tau = model.tau(test_df)
        err = np.mean([pehe(test_df[f"tau_{a}"].to_numpy(), tau[a]) for a in TREATED_ARMS])
        treated = (test_df.arm != "none").to_numpy()
        best_tau = np.maximum(tau["whatsapp"], tau["voice"])
        chosen = choose_arms(test_df, tau)
        policies[f"{name}_policy"] = true_net_value(test_df, chosen)
        out[name] = {"pehe": float(err), "qini": qini_auc(test_df.recovered.to_numpy(), treated, best_tau),
                     "policy_value_minor": policies[f"{name}_policy"]}
        if name == "advanced":
            mix = pd.Series(chosen).value_counts(normalize=True).to_dict()
    best_single = max(policies[f"always_{a}"] for a in ARMS)
    for name in out:
        out[name]["policy_gain_vs_best_single_arm"] = out[name]["policy_value_minor"] - best_single
    return UpliftReport(out["baseline"], out["advanced"], policies, {str(k): float(v) for k, v in mix.items()})
