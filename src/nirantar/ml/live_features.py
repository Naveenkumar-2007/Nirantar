"""Live feature adapter v1: build M1's serving features from the database for one debit.

Same point-in-time rule as training (only history known before T-3 days). Known approximations in v1,
each defaulting to the value the model saw for "no information" in training:
  - bank_td_rate_24h/7d = 0.0 (bank-health stream not persisted in the DB yet)
  - td_last6 / mandate_issue_last6 = 0 (per-code history not persisted; failures count toward nsf_last6)
  - rail = "upi_autopay" when the mandate rail is unknown; bank_id = -1 (unseen category → missing)
These are logged in the prediction output so monitoring can separate v1 approximations from model error.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.ml.cash_window import circular_distance, estimate
from nirantar.ml.features import CATEGORICAL, LEAD_DAYS, NUMERIC

ADAPTER_VERSION = "live-features-v1"


def features_for_debit(conn: Connection, tenant_id: str, debit_id: str) -> pd.DataFrame:
    d = conn.execute(
        text("SELECT d.debit_id, d.customer_id, d.scheduled_for, d.amount_minor, c.segment FROM billing.debits d "
             "JOIN billing.customers c ON c.tenant_id=d.tenant_id AND c.customer_id=d.customer_id "
             "WHERE d.tenant_id=:t AND d.debit_id=:d"), {"t": tenant_id, "d": debit_id}).one()
    cutoff = d.scheduled_for - timedelta(days=LEAD_DAYS)
    hist = conn.execute(
        text("SELECT scheduled_for, status, attempt_count FROM billing.debits WHERE tenant_id=:t AND customer_id=:c "
             "AND scheduled_for < :cut AND status IN ('succeeded','failed') ORDER BY scheduled_for"),
        {"t": tenant_id, "c": d.customer_id, "cut": cutoff}).all()
    outcomes = [0 if h.status == "succeeded" else 1 for h in hist]
    failures = [h for h in hist if h.status == "failed"]
    recovered = [h for h in hist if h.status == "succeeded" and h.attempt_count > 0]
    cw = estimate([h.scheduled_for.day for h in hist if h.status == "succeeded"],
                  [1.0 for h in hist if h.status == "succeeded"])
    row = {
        "debit_id": d.debit_id, "log_amount": float(np.log(d.amount_minor)),
        "tenure_months": (len({(h.scheduled_for.year, h.scheduled_for.month) for h in hist})),
        "n_prev": len(hist), "fail_rate_all": float(np.mean(outcomes)) if outcomes else 0.0,
        "fail_rate_last3": float(np.mean(outcomes[-3:])) if outcomes else 0.0,
        "last_failed": float(outcomes[-1]) if outcomes else 0.0,
        "days_since_last_failure": float((cutoff - failures[-1].scheduled_for).days) if failures else 999.0,
        "nsf_last6": sum(outcomes[-6:]), "td_last6": 0, "mandate_issue_last6": 0,
        "known_recovery_rate": (len(recovered) / (len(failures) + len(recovered))) if (failures or recovered) else -1.0,
        "cash_day_distance": circular_distance(d.scheduled_for.day, cw.day) if cw.day else 0.0,
        "cash_confidence": cw.confidence, "cash_n_obs": cw.n_obs,
        "bank_td_rate_24h": 0.0, "bank_td_rate_7d": 0.0,
        "segment": d.segment, "rail": "upi_autopay", "bank_id": -1,
    }
    df = pd.DataFrame([row])
    assert set(NUMERIC + CATEGORICAL) <= set(df.columns)
    df.attrs["adapter_version"] = ADAPTER_VERSION
    return df
