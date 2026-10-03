"""MLflow tracking/registry helpers and gate evaluation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow
from mlflow.tracking import MlflowClient

REPO = Path(__file__).resolve().parents[3]
GATES_FILE = REPO / "ml" / "gates.yaml"


def configure(tracking_dir: Path | None = None) -> str:
    base = tracking_dir or (REPO / "ml")
    base.mkdir(parents=True, exist_ok=True)
    uri = os.environ.get("MLFLOW_TRACKING_URI") or f"sqlite:///{(base / 'mlflow.db').as_posix()}"
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    return uri


def load_gates(model: str) -> dict[str, Any]:
    with GATES_FILE.open(encoding="utf-8") as f:
        return dict(yaml.safe_load(f)[model])


def evaluate_gates(model: str, advanced: dict[str, float], baseline: dict[str, float]) -> tuple[bool, list[str]]:
    """Return (passed, reasons). Lower-is-better metrics: brier, ece, pehe, wape."""
    gates = load_gates(model)
    lower_better = {"brier", "ece", "pehe", "wape"}
    failures: list[str] = []
    for key, threshold in gates.items():
        if key == "must_beat_baseline_on":
            for metric in threshold:
                a, b = advanced[metric], baseline[metric]
                worse = a > b if metric in lower_better else a < b
                if worse:
                    failures.append(f"{metric}: advanced {a:.4f} does not beat baseline {b:.4f}")
        elif key.startswith("min_"):
            metric = key[4:]
            if advanced.get(metric, float("-inf")) < threshold:
                failures.append(f"{metric} {advanced.get(metric, float('nan')):.4f} < {threshold}")
        elif key.startswith("max_"):
            metric = key[4:]
            if advanced.get(metric, float("inf")) > threshold:
                failures.append(f"{metric} {advanced.get(metric, float('nan')):.4f} > {threshold}")
    return not failures, failures


def promote(name: str, version: str) -> None:
    MlflowClient().set_registered_model_alias(name, "champion", version)


def champion_uri(name: str) -> str:
    return f"models:/{name}@champion"
