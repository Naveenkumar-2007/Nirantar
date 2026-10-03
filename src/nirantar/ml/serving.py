"""Model serving contract: load the champion, predict, and persist every prediction.

Every prediction is written to ai.predictions with model version and a hash of the
exact input features, so later labels can be joined back for monitoring/retraining
and any decision can be audited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.canonical import sha256_hex
from nirantar.core.ids import new_id
from nirantar.ml.features import CATEGORICAL, FEATURE_VERSION, NUMERIC


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    subject_id: str
    score: float
    model_name: str
    model_version: str


class Predictor:
    """Wraps any object with predict_proba(DataFrame) -> ndarray[:, 2]."""

    def __init__(self, model: Any, model_name: str, model_version: str) -> None:
        self.model = model
        self.model_name = model_name
        self.model_version = model_version

    @classmethod
    def from_registry(cls, model_name: str) -> Predictor:
        import mlflow

        from nirantar.ml import registry

        registry.configure()
        client = mlflow.tracking.MlflowClient()
        mv = client.get_model_version_by_alias(model_name, "champion")
        model = mlflow.sklearn.load_model(registry.champion_uri(model_name))
        return cls(model, model_name, str(mv.version))

    def predict_and_log(self, conn: Connection, tenant_id: str, rows: pd.DataFrame, subject_col: str,
                        now: datetime) -> list[Prediction]:
        missing = set(NUMERIC + CATEGORICAL) - set(rows.columns)
        if missing:
            raise ValueError(f"serving contract violated: missing features {sorted(missing)}")
        scores = self.model.predict_proba(rows)[:, 1]
        out = []
        for (_, row), score in zip(rows.iterrows(), scores, strict=True):
            feats = {k: (row[k].item() if hasattr(row[k], "item") else row[k]) for k in NUMERIC + CATEGORICAL}
            pid = new_id("prd")
            conn.execute(
                text("INSERT INTO ai.predictions (tenant_id, prediction_id, model_name, model_version, subject_id, "
                     "features_hash, score, output, predicted_at) VALUES (:t, :p, :m, :v, :s, :h, :sc, "
                     "CAST(:o AS jsonb), :n)"),
                {"t": tenant_id, "p": pid, "m": self.model_name, "v": self.model_version,
                 "s": str(row[subject_col]), "h": sha256_hex({"v": FEATURE_VERSION, "f": feats}),
                 "sc": float(score), "o": json.dumps({"feature_version": FEATURE_VERSION}), "n": now},
            )
            out.append(Prediction(pid, str(row[subject_col]), float(score), self.model_name, self.model_version))
        return out
