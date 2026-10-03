"""Model specifications: everything the generic lifecycle (training → gates → shadow/canary/champion → monitoring)
needs to know about one per-tenant model. Adding a model = adding a spec, not another pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from nirantar.data.lake import Lake
from nirantar.features.offline import TRAINING_TABLE as M1_TABLE
from nirantar.features.offline import build_training_set
from nirantar.ml import churn
from nirantar.ml.models.m1_tenant import PriorModel


@dataclass(frozen=True)
class ModelSpec:
    name: str
    title: str
    readiness_key: str                    # data/readiness.yaml
    gates_key: str                        # ml/gates.yaml offline gates
    training_table: str
    label_name: str                       # live label in ai.labels joined to predictions by subject_id
    label_source: str
    prior_version: str                    # version string of the transparent prior in ai.predictions
    build: Callable[[Lake, str, datetime, str], pd.DataFrame]
    baseline: Callable[[Lake, str, pd.DataFrame, np.ndarray], Any]
    growth: Callable[[dict[str, Any]], int]   # labelled-evidence count from a data-health report


def _m1_build(lake: Lake, tenant_id: str, now: datetime, source: str) -> pd.DataFrame:
    return build_training_set(lake, tenant_id, now=now, source=source)


def _m6_build(lake: Lake, tenant_id: str, now: datetime, source: str) -> pd.DataFrame:
    _m1_build(lake, tenant_id, now, source)               # snapshots first (same features, same function)
    return churn.build_m6_training_set(lake, tenant_id, now=now, source=source)


def _m13_build(lake: Lake, tenant_id: str, now: datetime, source: str) -> pd.DataFrame:
    _m1_build(lake, tenant_id, now, source)
    return churn.build_m13_training_set(lake, tenant_id, now=now, source=source)


M1 = ModelSpec("m1_debit_failure", "Debit failure risk", "m1_debit_failure", "m1_debit_failure_tenant", M1_TABLE,
               "debit_failed", "verifier", "prior-v1", _m1_build, lambda _l, _t, _tr, _y: PriorModel(),
               lambda rep: int(rep["labels"]["attempted_cycles"]))
M6 = ModelSpec("m6_churn", "Churn within 60 days", "m6_churn", "m6_churn_tenant", churn.M6_TABLE,
               "churned_60d", "lifecycle", "sbg-prior", _m6_build, churn.baseline_m6,
               lambda rep: int(rep["subscriptions"]["churned"]))
M13 = ModelSpec("m13_churn_type", "Churn reason (payment trouble vs choice)", "m13_churn_type",
                "m13_churn_type_tenant", churn.M13_TABLE, "churn_involuntary", "lifecycle", "rule-prior", _m13_build,
                churn.baseline_m13, lambda rep: int(rep["subscriptions"]["churned"]))
SPECS: dict[str, ModelSpec] = {s.name: s for s in (M1, M6, M13)}


def spec(name: str) -> ModelSpec:
    if name not in SPECS:
        raise ValueError(f"unknown model {name!r}")
    return SPECS[name]
