"""Workflow/activity payloads. Plain dataclasses: safe for Temporal's deterministic sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CycleInput:
    tenant_id: str
    debit_id: str
    customer_id: str
    experiment_id: str
    debit_at_iso: str                 # when the provider executes the debit (UTC ISO-8601)
    payment_wait_hours: int = 36      # no webhook by then → reconcile against the provider
    recovery_window_days: int = 7
    round_gap_days: int = 3           # time between recovery rounds
    max_contact_rounds: int = 2


@dataclass
class StepInput:
    cycle: CycleInput
    now_iso: str                      # workflow time: activities never read the wall clock for decisions
    case_id: str
    payload: dict[str, Any] = field(default_factory=dict)
