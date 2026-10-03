"""Contact Arbiter — deterministic optimiser (BB-§16). Not an LLM.

For a batch of failed debits, choose at most one intervention per customer to
maximise expected incremental value, subject to:
  - per-arm eligibility (consent, contact window, fatigue, TRAI registration) already decided by the
    Compliance Guardian — ineligible arms are simply absent from `allowed_arms`
  - channel capacity (e.g. concurrent voice slots, WhatsApp throughput)
  - holdout: customers assigned to the holdout get no optional intervention

Value(arm) = amount * effect(arm | failure_code) - cost(arm).
Effects and costs come from `EffectPriors`, built by the caller from tenant configuration (ADR-0011): the tenant's
own learned effects (holdout-based, `nirantar.settings.learning`) or its configured priors, and its channel costs.
M5's per-customer estimates can be passed in via `value_override` once M5 is promoted (ADR-0007).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

ARMS = ("whatsapp", "voice")


@dataclass(frozen=True)
class EffectPriors:
    effects: Mapping[str, Mapping[str, float]]     # failure category → channel → incremental recovery probability
    cost_minor: Mapping[str, int]                  # channel → cost of one contact

    def value(self, amount_minor: int, failure_code: str, arm: str) -> int:
        eff = self.effects.get(failure_code, self.effects["UNKNOWN"]).get(arm, 0.0)
        return round(amount_minor * eff) - int(self.cost_minor[arm])


@dataclass(frozen=True)
class Candidate:
    case_id: str
    customer_id: str
    amount_minor: int
    failure_code: str
    allowed_arms: frozenset[str]          # after Compliance Guardian pre-check
    in_holdout: bool = False
    value_override: Mapping[str, int] | None = None   # e.g. from a promoted uplift model


@dataclass(frozen=True)
class Assignment:
    case_id: str
    customer_id: str
    arm: str | None                        # None = no optional contact
    expected_value_minor: int
    reason: str


def arbitrate(candidates: Sequence[Candidate], capacity: Mapping[str, int],
              priors: EffectPriors) -> list[Assignment]:
    model = cp_model.CpModel()
    x: dict[tuple[int, str], cp_model.IntVar] = {}
    values: dict[tuple[int, str], int] = {}
    for i, c in enumerate(candidates):
        if c.in_holdout:
            continue
        for arm in ARMS:
            if arm not in c.allowed_arms:
                continue
            v = int(c.value_override[arm]) if c.value_override and arm in c.value_override else \
                priors.value(c.amount_minor, c.failure_code, arm)
            if v <= 0:
                continue  # never spend a contact that isn't expected to pay for itself
            x[i, arm] = model.new_bool_var(f"x_{i}_{arm}")
            values[i, arm] = v
    for i in range(len(candidates)):
        arms = [x[i, a] for a in ARMS if (i, a) in x]
        if arms:
            model.add(sum(arms) <= 1)
    by_customer: dict[str, list[cp_model.IntVar]] = {}
    for (i, _a), var in x.items():
        by_customer.setdefault(candidates[i].customer_id, []).append(var)
    for vars_ in by_customer.values():
        model.add(sum(vars_) <= 1)  # one conversation per customer per round
    for arm in ARMS:
        vars_ = [var for (_i, a), var in x.items() if a == arm]
        if vars_:
            model.add(sum(vars_) <= int(capacity.get(arm, 0)))
    model.maximize(sum(values[k] * var for k, var in x.items()))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1       # deterministic
    solver.parameters.random_seed = 7
    solver.parameters.max_time_in_seconds = 5.0
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"arbiter could not solve: {solver.status_name(status)}")
    out = []
    for i, c in enumerate(candidates):
        chosen = next((a for a in ARMS if (i, a) in x and solver.value(x[i, a])), None)
        if c.in_holdout:
            reason = "holdout: optional interventions withheld"
        elif chosen:
            reason = f"max expected value under capacity ({chosen})"
        elif not any((i, a) in x for a in ARMS):
            reason = "no eligible arm with positive expected value"
        else:
            reason = "lost capacity to higher-value cases"
        out.append(Assignment(c.case_id, c.customer_id, chosen, values.get((i, chosen), 0) if chosen else 0, reason))
    return out
