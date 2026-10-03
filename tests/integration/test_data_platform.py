"""Data platform on real services: Postgres (RLS, change capture) + Iceberg on SeaweedFS S3 + Dagster."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from sqlalchemy import Engine, text

from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.data import bronze, health
from nirantar.data.lake import Lake
from nirantar.data.pipeline import run_tenant
from nirantar.data.sources import MockSource
from nirantar.db.session import tenant_tx
from nirantar.payments.providers.mock import MockProvider

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(microsecond=0)


def _tenant_with_history(engine: Engine, days: int = 60) -> tuple[str, MockProvider]:
    """A tenant whose provider holds `days` of subscription charges (some failing, some recovered)."""
    t, mock = new_id("ten"), MockProvider()
    with tenant_tx(t, engine) as c:
        create_tenant(c, t, "History Co", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:x", "literal:y")
        for i in range(6):
            cust = create_customer(c, t, NewCustomer(f"e{i}", f"Customer {i}", f"+9198765432{i:02d}",
                                                     f"c{i}@example.com", "en", consents={"sms": True}))
            sub = mock.add_subscription(cust, Money.of("499"))
            create_subscription(c, t, cust, "mock", sub, Money.of("499"))
            for k in range(days // 30):
                at = NOW - timedelta(days=days - 30 * k)
                ok = (i + k) % 3 != 0
                mock.charge(sub, succeed=ok, at=at)
                if not ok and i % 2 == 0:                   # half the failures recover two days later
                    mock.charge(sub, succeed=True, at=at + timedelta(days=2))
    return t, mock


def test_full_pipeline_backfill_to_gold_and_health(app_engine: Engine, lake: Lake) -> None:
    t, mock = _tenant_with_history(app_engine)
    out = run_tenant(app_engine, lake, t, now=NOW, trigger="test", sources=[MockSource(mock)],
                     backfill_since=NOW - timedelta(days=90))
    assert out["status"] == "succeeded"
    steps = {s["step"]: s["result"] for s in out["steps"]}
    assert steps["bronze.backfill.mock"]["detail"]["payments"]["rows"] == len(mock.payments)
    # PII never lands in the lake: phones/emails/names from billing.customers are absent from bronze
    raw = lake.read(bronze.CHANGES_TABLE, t)
    blob = " ".join(raw.row)
    assert "+9198765432" not in blob and "@example.com" not in blob and "Customer 0" not in blob
    co = lake.read("gold.charge_outcomes", t)
    failed = co[co.first_attempt_failed.fillna(False).astype(bool)]
    assert len(co) == 12 and len(failed) == 4 and int(failed.recovered.sum()) == 2
    assert set(co.source) == {"subscription_payments"}
    rep = health.latest(app_engine, t)
    assert rep is not None and rep["labels"]["cycles"] == 12
    assert rep["readiness"]["m1_debit_failure"]["ready"] is False                     # honest: far too little
    assert rep["readiness"]["m1_debit_failure"]["gaps"]["attempted_cycles"] == {"need": 2000, "have": 12}
    assert rep["labels"]["failure_rate"] == round(4 / 12, 4)
    assert rep["sources"][0]["provider"] == "mock"

    # ---- re-run: settled windows are skipped, nothing new captured, gold identical
    again = run_tenant(app_engine, lake, t, now=NOW, trigger="test", sources=[MockSource(mock)],
                       backfill_since=NOW - timedelta(days=90))
    s2 = {s["step"]: s["result"] for s in again["steps"]}
    assert s2["bronze.backfill.mock"]["detail"]["payments"]["windows_skipped"] >= 10
    assert s2["bronze.changes"]["rows"] == 0
    co2 = lake.read("gold.charge_outcomes", t)
    assert sorted(co2.cycle_id) == sorted(co.cycle_id)


def test_change_capture_never_skips_a_transaction_that_commits_late(app_engine: Engine, lake: Lake) -> None:
    t = new_id("ten")
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "CDC Co")
        cust = create_customer(c, t, NewCustomer("e1", "A", None, None, "en"))
        sub = create_subscription(c, t, cust, "mock", "sub_x", Money.of("100"))
        schedule_debit(c, t, sub, date(2026, 9, 30), NOW)   # creates the tenant's ledger accounts up front,
    bronze.extract_changes(app_engine, lake, t, run_id="r1", now=NOW)   # so the two writers below don't contend
    # a writer opens a transaction and inserts a debit, but has NOT committed when extraction runs
    slow = app_engine.connect()
    try:
        tx = slow.begin()
        slow.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t})
        slow.execute(text("SET LOCAL lock_timeout = '5s'"))
        late_debit = schedule_debit(slow, t, sub, date(2026, 10, 1), NOW)
        # meanwhile a fast writer commits another debit
        with tenant_tx(t, app_engine) as c:
            c.execute(text("SET LOCAL lock_timeout = '5s'"))
            fast_debit = schedule_debit(c, t, sub, date(2026, 10, 2), NOW)
        r2 = bronze.extract_changes(app_engine, lake, t, run_id="r2", now=NOW)
        tx.commit()
    finally:
        slow.close()
    r3 = bronze.extract_changes(app_engine, lake, t, run_id="r3", now=NOW)
    changes = lake.read(bronze.CHANGES_TABLE, t)
    debits_by_run = {run: set(g[g.source_table == "billing.debits"].pk) for run, g in changes.groupby("run_id")}
    assert fast_debit in debits_by_run["r2"] and late_debit not in debits_by_run["r2"]
    assert late_debit in debits_by_run["r3"], "the late-committing row must be captured by the next run"
    assert r2.detail["billing.debits"] >= 1 and r3.detail["billing.debits"] >= 1


def test_lake_is_tenant_isolated(app_engine: Engine, lake: Lake) -> None:
    a, b = new_id("ten"), new_id("ten")
    rows = pa.Table.from_pylist([{"tenant_id": a, "source_table": "x", "pk": "1", "row": "{}", "cdc_txid": 1,
                                  "ingested_at": NOW, "run_id": "r"}], schema=bronze.CHANGES_SCHEMA)
    lake.append(bronze.CHANGES_TABLE, a, rows)
    assert lake.read(bronze.CHANGES_TABLE, b).empty
    with pytest.raises(ValueError, match="other tenants"):
        lake.append(bronze.CHANGES_TABLE, b, rows)          # b cannot write a's rows
    with pytest.raises(ValueError, match="other tenants"):
        lake.replace_tenant("silver.quarantine", b, pa.Table.from_pylist(
            [{"tenant_id": a, "table_name": "x", "reason": "r", "row": "{}", "run_id": "r", "quarantined_at": NOW}]))


def test_corrupt_source_rows_are_quarantined_not_trained_on(app_engine: Engine, lake: Lake) -> None:
    t, mock = _tenant_with_history(app_engine, days=30)
    bad = {"id": "pay_corrupt", "entity": "payment", "amount": -100, "currency": "INR", "status": "teleported",
           "created_at": int((NOW - timedelta(days=3)).timestamp())}

    class Corrupting(MockSource):
        def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
            yield from super().list(entity, since, until)
            if entity == "payments" and since <= NOW - timedelta(days=3) < until:
                yield bad

    run_tenant(app_engine, lake, t, now=NOW, trigger="test", sources=[Corrupting(mock)],
               backfill_since=NOW - timedelta(days=45))
    q = lake.read("silver.quarantine", t)
    row = q[q.row.str.contains("pay_corrupt")]
    assert len(row) == 1 and "amount_minor" in row.reason.iloc[0] and "status" in row.reason.iloc[0]
    assert "pay_corrupt" not in set(lake.read("silver.provider_payments", t).payment_id)
    assert health.latest(app_engine, t)["quarantine"]["rows"] == 1  # type: ignore[index]


def test_dagster_assets_materialize_with_passing_checks(app_engine: Engine, lake: Lake) -> None:
    import dagster as dg

    from nirantar.data import orchestration as o

    t, _ = _tenant_with_history(app_engine, days=30)
    inst = dg.DagsterInstance.ephemeral()
    inst.add_dynamic_partitions("tenants", [t])
    res = dg.materialize([o.bronze_provider_history, o.bronze_nirantar_changes, o.silver_tables,
                          o.gold_charge_outcomes, o.data_health], partition_key=t, instance=inst)
    assert res.success
    assert all(e.passed for e in res.get_asset_check_evaluations())
    with tenant_tx(t, app_engine) as c:
        run = c.execute(text("SELECT trigger, status, steps FROM ingest.pipeline_runs WHERE tenant_id=:t"),
                        {"t": t}).one()
    steps = run.steps if isinstance(run.steps, list) else json.loads(run.steps)
    assert run.trigger == "dagster" and run.status == "succeeded"
    assert [s["step"] for s in steps] == ["bronze.changes", "silver", "gold.charge_outcomes",
                                          "gold.subscription_lifecycle", "health"]


def test_tenant_sensor_onboards_in_bounded_batches(app_engine: Engine) -> None:
    import dagster as dg

    from nirantar.data import orchestration as o

    inst = dg.DagsterInstance.ephemeral()
    result = o.tenants_sensor(dg.build_sensor_context(instance=inst))
    assert isinstance(result, dg.SensorResult)
    assert result.run_requests is not None
    assert 0 < len(result.run_requests) <= o.MAX_NEW_TENANTS_PER_TICK
    add = result.dynamic_partitions_requests[0]
    assert isinstance(add, dg.AddDynamicPartitionsRequest)
    assert sorted(add.partition_keys) == sorted(r.partition_key for r in result.run_requests)
