from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.experiments.service import (
    HOLDOUT,
    ExperimentPolicyError,
    analyze,
    assign,
    create_experiment,
    is_withholdable,
    record_outcome,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _tenant(engine: Engine, settings: dict[str, object]) -> str:
    t = new_id("ten")
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, "x", settings)
    return t


def test_assignment_is_stable_and_roughly_proportional(app_engine: Engine) -> None:
    t = _tenant(app_engine, {"segments": ["subscription"]})
    with tenant_tx(t, app_engine) as c:
        exp = create_experiment(c, t, "recovery-v1", {"whatsapp": 4600, "voice": 4600})  # default 8% holdout
        arms = [assign(c, t, exp, f"cus_{i}", NOW) for i in range(3000)]
        again = [assign(c, t, exp, f"cus_{i}", NOW) for i in range(3000)]
    assert arms == again
    share = arms.count(HOLDOUT) / len(arms)
    assert 0.06 < share < 0.10


def test_lending_tenant_holdout_requires_opt_in(app_engine: Engine) -> None:
    lender = _tenant(app_engine, {"segments": ["lending"]})
    with tenant_tx(lender, app_engine) as c:
        exp = create_experiment(c, lender, "collections", {"voice": 10_000})  # default: no holdout
        assert all(assign(c, lender, exp, f"c{i}", NOW) == "voice" for i in range(200))
        with pytest.raises(ExperimentPolicyError):
            create_experiment(c, lender, "collections-holdout", {"voice": 9500}, holdout_bp=500)
    opted = _tenant(app_engine, {"segments": ["lending"], "holdout_opt_in": True})
    with tenant_tx(opted, app_engine) as c:
        create_experiment(c, opted, "collections-holdout", {"voice": 9500}, holdout_bp=500)


def test_mandatory_actions_are_never_withheld() -> None:
    assert not is_withholdable("predebit_notice")
    assert not is_withholdable("hardship_response")
    assert is_withholdable("whatsapp_recovery_nudge")


def test_incrementality_uses_only_verified_outcomes(app_engine: Engine) -> None:
    t = _tenant(app_engine, {"segments": ["subscription"]})
    rng = random.Random(3)
    with tenant_tx(t, app_engine) as c:
        exp = create_experiment(c, t, "v", {"voice": 5000}, holdout_bp=5000)
        for i in range(800):
            cid = f"cus_{i}"
            arm = assign(c, t, exp, cid, NOW)
            p = 0.55 if arm == "voice" else 0.40
            if rng.random() < p:
                record_outcome(c, t, exp, cid, None, "recovered", 99900, True, NOW)
            # unverified claims must not count
            record_outcome(c, t, exp, cid, None, "recovered", 99900, False, NOW)
        res = analyze(c, t, exp)
    inc = res["incremental"]["voice"]
    assert 0.07 < inc["incremental_recovery_rate"] < 0.23
    assert inc["ci95"][0] < inc["incremental_recovery_rate"] < inc["ci95"][1]
    assert inc["significant"]
    assert res["arms"][HOLDOUT]["recovery_rate"] < 0.5  # unverified outcomes were ignored
