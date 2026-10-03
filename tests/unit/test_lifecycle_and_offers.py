"""Churn lifecycle labelling rules and win-back offer arithmetic (no services)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from nirantar.data import lifecycle
from nirantar.mcp.tools import offer_terms

NOW = datetime(2026, 10, 1, tzinfo=UTC)
T = "ten_01TESTLIFECYCLE00000000AA"


class MemLake:
    def __init__(self) -> None:
        self.tables: dict[str, pd.DataFrame] = {}

    def read(self, name: str, tenant_id: str, columns: tuple[str, ...] | None = None) -> pd.DataFrame:
        return self.tables.get(name, pd.DataFrame())

    def replace_tenant(self, name: str, tenant_id: str, data: pa.Table) -> int:
        self.tables[name] = data.to_pandas()
        return data.num_rows


def _cycles(sub: str, n: int, last_days_ago: int, *, last_failed: bool = False, recovered: bool = False,
            period: int = 30) -> list[dict[str, Any]]:
    out = []
    for k in range(n):
        at = NOW - timedelta(days=last_days_ago + period * (n - 1 - k))
        failed = last_failed and k == n - 1
        paid = not failed or recovered
        out.append({"cycle_id": f"{sub}:{k}", "subscription_id": sub, "customer_id": f"c_{sub}",
                    "first_attempt_at": pd.Timestamp(at), "first_attempt_failed": failed, "paid": paid,
                    "amount_minor": 49900, "tenant_id": T})
    return out


def _run(rows: list[dict[str, Any]], provider: list[dict[str, Any]] | None = None) -> pd.DataFrame:
    lake = MemLake()
    lake.tables["gold.charge_outcomes"] = pd.DataFrame(rows)
    if provider:
        lake.tables["silver.provider_subscriptions"] = pd.DataFrame(provider)
    lifecycle.build_lifecycle(lake, T, now=NOW)  # type: ignore[arg-type]
    return lake.tables[lifecycle.TABLE].set_index("entity_id")


def test_lifecycle_labels_active_voluntary_involuntary_and_completed() -> None:
    rows = (_cycles("active", 6, 10) + _cycles("quiet_paid", 6, 60)
            + _cycles("quiet_failed", 6, 60, last_failed=True) + _cycles("late_recovered", 6, 60, last_failed=True,
                                                                       recovered=True)
            + _cycles("halted", 4, 5, last_failed=True) + _cycles("done", 12, 5))
    provider = [{"subscription_id": "halted", "status": "halted", "ended_at": pd.Timestamp(NOW - timedelta(days=2))},
                {"subscription_id": "done", "status": "completed", "ended_at": pd.Timestamp(NOW - timedelta(days=1))}]
    lc = _run(rows, provider)
    assert lc.loc["active", "status"] == "active" and pd.isna(lc.loc["active", "churn_type"])
    assert (lc.loc["quiet_paid", "status"], lc.loc["quiet_paid", "churn_type"]) == ("churned", "voluntary")
    assert (lc.loc["quiet_failed", "status"], lc.loc["quiet_failed", "churn_type"]) == ("churned", "involuntary")
    assert lc.loc["late_recovered", "churn_type"] == "voluntary"          # the failure WAS recovered
    assert lc.loc["quiet_paid", "churn_source"] == "inferred"
    assert lc.loc["quiet_paid", "churn_at"] == pd.Timestamp(NOW - timedelta(days=30))   # the missed next due
    assert (lc.loc["halted", "status"], lc.loc["halted", "churn_type"], lc.loc["halted", "churn_source"]) == \
        ("churned", "involuntary", "provider")
    assert lc.loc["done", "status"] == "completed" and pd.isna(lc.loc["done", "churn_type"])   # not churn
    assert lc.loc["active", "paid_cycles"] == 6 and lc.loc["quiet_failed", "paid_cycles"] == 5


def test_a_quiet_subscriber_inside_the_grace_period_is_still_active() -> None:
    # 1.5 periods + 7 days = 52 days of silence before a monthly subscriber counts as churned
    lc = _run(_cycles("s", 5, 50) + _cycles("t", 5, 53))
    assert lc.loc["s", "status"] == "active" and lc.loc["t", "status"] == "churned"


def test_weekly_plans_get_their_own_period() -> None:
    lc = _run(_cycles("w", 8, 5, period=7))
    assert lc.loc["w", "period_days"] == pytest.approx(7.0) and lc.loc["w", "monthly_value_minor"] == 213857


@pytest.mark.parametrize(("amount", "pct", "expected"), [(49900, 0, 49900), (49900, 10, 44900), (49900, 25, 37400),
                                                         (99900, 50, 50000), (1000, 90, 100)])
def test_offer_amount_is_computed_by_code_in_whole_rupees(amount: int, pct: int, expected: int) -> None:
    assert offer_terms(amount, pct) == expected
