"""Training pipelines: data → features → train → evaluate → gate → register → card.

Run:  uv run python -m nirantar.ml.pipelines --customers 4000 --months 12
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import mlflow

from nirantar.ml import registry
from nirantar.ml.features import build_features
from nirantar.ml.models import m1_debit_failure as m1
from nirantar.ml.models import m4_bank_health as m4
from nirantar.ml.models import m5_uplift as m5
from nirantar.ml.models import m10_cash_forecast as m10
from nirantar.ml.recursim import SimConfig, SimResult, simulate

CARDS = registry.REPO / "ml" / "model-cards"


def run_m1(sim: SimResult, feats: Any, register: bool = True) -> tuple[dict[str, Any], m1.M1Model]:
    result = m1.train(feats)
    passed, reasons = registry.evaluate_gates(m1.MODEL_NAME, result.advanced, result.baseline)
    provenance = {"source": "recursim", "seed": sim.config.seed, "customers": sim.config.n_customers,
                  "months": sim.config.months}
    version = None
    with mlflow.start_run(run_name="m1_train") as run:
        mlflow.set_tags({"model": m1.MODEL_NAME, "data_source": "recursim", "gate_passed": str(passed)})
        mlflow.log_params({**provenance, "feature_version": feats.attrs["feature_version"]})
        mlflow.log_metrics({f"baseline_{k}": v for k, v in result.baseline.items()})
        mlflow.log_metrics({f"advanced_{k}": v for k, v in result.advanced.items()})
        mlflow.log_dict(result.importance, "feature_importance.json")
        info = mlflow.sklearn.log_model(result.model, name="model",
                                        registered_model_name=m1.MODEL_NAME if register else None,
                                        skops_trusted_types=m1.TRUSTED_TYPES)
        if register and passed and info.registered_model_version is not None:
            version = str(info.registered_model_version)
            registry.promote(m1.MODEL_NAME, version)
        run_id = run.info.run_id
    CARDS.mkdir(parents=True, exist_ok=True)
    (CARDS / "m1_debit_failure.md").write_text(m1.model_card(result, passed, reasons, provenance), encoding="utf-8")
    return ({"model": m1.MODEL_NAME, "run_id": run_id, "gate_passed": passed, "reasons": reasons,
             "champion_version": version, "baseline": result.baseline, "advanced": result.advanced},
            result.model)


def run_m4(sim: SimResult) -> dict[str, Any]:
    base, adv = m4.run(sim.bank_hourly)
    b, a = asdict(base), asdict(adv)
    ok_base, why_base = registry.evaluate_gates("m4_bank_health", b, b)
    ok_adv, why_adv = registry.evaluate_gates("m4_bank_health", a, a)
    # ADR-0007: prefer the higher-recall detector among those passing the gate
    champion = "ewma" if ok_base and (not ok_adv or b["incident_recall"] >= a["incident_recall"]) else \
        ("bocpd" if ok_adv else None)
    with mlflow.start_run(run_name="m4_eval"):
        mlflow.set_tags({"model": "m4_bank_health", "data_source": "recursim", "champion": str(champion)})
        mlflow.log_metrics({f"ewma_{k}": float(v) for k, v in b.items()})
        mlflow.log_metrics({f"bocpd_{k}": float(v) for k, v in a.items()})
    return {"model": "m4_bank_health", "ewma": b, "bocpd": a, "champion": champion,
            "gate_reasons": {"ewma": why_base, "bocpd": why_adv}}


def run_m5(sim: SimResult) -> dict[str, Any]:
    df = m5.uplift_frame(sim.customers, sim.debits, sim.failures)
    cut = sim.config.months - 3
    train, test = df[df.month < cut], df[df.month >= cut]
    propensity = dict(zip(m5.ARMS, sim.config.arm_probs, strict=True))
    rep = m5.evaluate(train, test, propensity)
    cap = m5.evaluate_capacity(train, test, propensity, voice_capacity=0.2)
    advanced = {**rep.advanced, "capacity_gain_vs_amount_heuristic": cap["dr_learner"] - cap["largest_amount"]}
    passed, reasons = registry.evaluate_gates("m5_uplift", advanced, rep.baseline)
    with mlflow.start_run(run_name="m5_eval"):
        mlflow.set_tags({"model": "m5_uplift", "data_source": "recursim", "gate_passed": str(passed),
                         "mode": "promoted" if passed else "shadow"})
        mlflow.log_metrics({f"capacity_{k}": v for k, v in cap.items()})
        mlflow.log_metrics({f"advanced_{k}": v for k, v in advanced.items()})
    return {"model": "m5_uplift", "gate_passed": passed, "mode": "promoted" if passed else "shadow",
            "reasons": reasons, "baseline": rep.baseline, "advanced": advanced,
            "policy_values_minor": rep.policy_values_minor, "capacity_20pct_minor": cap}


def run_m10(sim: SimResult, m1_model: m1.M1Model, feats: Any) -> dict[str, Any]:
    parts = m1.temporal_split(feats)
    va, te = parts["valid"], parts["test"]
    rep = m10.evaluate(m10.daily_frame(va, m1_model.predict_proba(va)[:, 1]),
                       m10.daily_frame(te, m1_model.predict_proba(te)[:, 1]))
    passed, reasons = registry.evaluate_gates("m10_cash_forecast", rep.advanced, rep.baseline)
    with mlflow.start_run(run_name="m10_eval"):
        mlflow.set_tags({"model": "m10_cash_forecast", "data_source": "recursim", "gate_passed": str(passed)})
        mlflow.log_metrics({f"advanced_{k}": v for k, v in rep.advanced.items()})
        mlflow.log_metrics({f"baseline_{k}": v for k, v in rep.baseline.items()})
    return {"model": "m10_cash_forecast", "gate_passed": passed, "reasons": reasons, "baseline": rep.baseline,
            "advanced": rep.advanced, "test_days": rep.days}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=4000)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    registry.configure()
    mlflow.set_experiment("nirantar-recursim")
    sim = simulate(SimConfig(n_customers=args.customers, months=args.months, seed=args.seed))
    feats = build_features(sim.customers, sim.debits, sim.failures, sim.bank_hourly)
    m1_report, m1_model = run_m1(sim, feats)
    report = {"data_source": "recursim", "config": asdict(sim.config), "m1": m1_report, "m4": run_m4(sim),
              "m5": run_m5(sim), "m10": run_m10(sim, m1_model, feats)}
    out = Path(registry.REPO / "evals" / "results" / "ml_latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
