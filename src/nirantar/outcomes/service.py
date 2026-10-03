"""Close a debit cycle with a VERIFIED outcome and turn it into learning signal.

One transaction writes: labels (joined to the prediction that was made), the experiment outcome,
the outcome.* event, and the case closure. Unverified outcomes are recorded as such and never used as
positive training labels or incrementality evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.contracts.events import make_event
from nirantar.core.ids import new_id
from nirantar.db.stores import Outbox
from nirantar.experiments.service import record_outcome

OUTCOMES = ("paid_on_time", "recovered", "unrecovered", "unverified")


@dataclass(frozen=True)
class ClosedCycle:
    debit_id: str
    outcome: str
    verified: bool
    label_ids: list[str]
    outcome_id: str | None
    event_id: str


def close_cycle(conn: Connection, tenant_id: str, debit_id: str, customer_id: str, outcome: str, verified: bool,
                value_minor: int, prediction_id: str | None, experiment_id: str | None, case_id: str | None,
                now: datetime) -> ClosedCycle:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome}")
    labels = []
    if verified:
        for name, value in (("debit_failed", outcome != "paid_on_time"),
                            ("recovered_within_window", outcome == "recovered")):
            lid = new_id("lbl")
            conn.execute(
                text("INSERT INTO ai.labels (tenant_id, label_id, prediction_id, subject_id, label_name, value, "
                     "source, "
                     "observed_at) VALUES (:t, :l, :p, :s, :n, CAST(:v AS jsonb), 'verifier', :o)"),
                {"t": tenant_id, "l": lid, "p": prediction_id, "s": debit_id, "n": name,
                 "v": json.dumps({"value": value}), "o": now})
            labels.append(lid)
    oid = None
    if experiment_id:
        oid = record_outcome(conn, tenant_id, experiment_id, customer_id, debit_id,
                             "recovered" if outcome in ("recovered", "paid_on_time") else outcome, value_minor,
                             verified, now)
    evt = make_event(event_type=f"outcome.{outcome}", version=1, tenant_id=tenant_id, subject_id=debit_id,
                     payload={"debit_id": debit_id, "customer_id": customer_id, "verified": verified,
                              "value_minor": value_minor, "label_ids": labels, "case_id": case_id},
                     source="outcomes", occurred_at=now)
    Outbox(conn).add(evt)
    if case_id:
        conn.execute(text("UPDATE ops.cases SET status='closed', closed_at=:n, summary = summary || CAST(:s AS jsonb) "
                          "WHERE tenant_id=:t AND case_id=:c"),
                     {"n": now, "s": json.dumps({"outcome": outcome, "verified": verified}), "t": tenant_id,
                      "c": case_id})
    return ClosedCycle(debit_id, outcome, verified, labels, oid, evt.event_id)
