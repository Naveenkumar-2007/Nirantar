"""Data platform logic without services: PII redaction, contracts, silver typing, gold billing cycles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from nirantar.data import gold, silver
from nirantar.data.bronze import _windows
from nirantar.data.contracts import ContractError, enforce
from nirantar.data.pii import redact

NOW = datetime(2026, 9, 29, tzinfo=UTC)
T = "ten_01TESTDATAPLATFORM000000A"


class MemLake:
    """Stand-in for Lake with the same tenant-scoped read/replace contract."""

    def __init__(self) -> None:
        self.tables: dict[str, pd.DataFrame] = {}

    def read(self, name: str, tenant_id: str, columns: tuple[str, ...] | None = None) -> pd.DataFrame:
        df = self.tables.get(name, pd.DataFrame())
        return df[df.tenant_id == tenant_id].reset_index(drop=True) if not df.empty else df

    def replace_tenant(self, name: str, tenant_id: str, data: pa.Table) -> int:
        self.tables[name] = data.to_pandas()
        return data.num_rows


# ------------------------------------------------------------------ PII
def test_redaction_hashes_personal_data_and_keeps_structure() -> None:
    raw = {"id": "pay_1", "amount": 99900, "email": "priya@example.com", "contact": "+919876543210",
           "vpa": "priya@upi", "card": {"last4": "1111", "name": "PRIYA"},
           "notes": {"nirantar_ref": "deb_1.1", "customer_note": "call my office"},
           "acquirer_data": {"upi_transaction_id": "123"}, "upi": {"vpa": "priya@upi"}}
    out = redact(raw, T)
    flat = str(out)
    for secret in ("priya@example.com", "+919876543210", "priya@upi", "PRIYA", "call my office"):
        assert secret not in flat
    assert out["amount"] == 99900 and out["notes"]["nirantar_ref"] == "deb_1.1"
    assert out["acquirer_data"] == {"upi_transaction_id": "123"}
    assert redact(raw, T) == out                                       # deterministic → joinable
    assert redact(raw, "ten_01OTHERTENANT00000000000")["email"] != out["email"]   # tenant-scoped


# ------------------------------------------------------------------ contracts
def _debit(i: int, **kw: Any) -> dict[str, Any]:
    base = {"tenant_id": T, "debit_id": f"deb_{i}", "subscription_id": "sub_1", "customer_id": "cus_1",
            "scheduled_for": pd.Timestamp("2026-09-01", tz="UTC"), "amount_minor": 99900, "currency": "INR",
            "status": "succeeded", "attempt_count": 1, "provider_payment_id": None, "last_error_code": None,
            "created_at": pd.Timestamp("2026-08-28", tz="UTC"), "updated_at": pd.Timestamp("2026-09-01", tz="UTC")}
    return {**base, **kw}


def test_bad_rows_are_quarantined_with_reasons() -> None:
    df = silver._typed(T, [_debit(1), _debit(2, amount_minor=-5), _debit(3, status="teleported"),
                           _debit(4, scheduled_for="not-a-date")], "silver.debits")
    checked = enforce("silver.debits", df)
    assert list(checked.valid.debit_id) == ["deb_1"]
    reasons = dict(zip(checked.quarantined.debit_id, checked.quarantined.reason, strict=True))
    assert "amount_minor" in reasons["deb_2"] and "status" in reasons["deb_3"] and "scheduled_for" in reasons["deb_4"]


def test_missing_columns_fail_the_whole_table() -> None:
    df = silver._typed(T, [_debit(1)], "silver.debits").drop(columns=["status"])
    with pytest.raises(ContractError):
        enforce("silver.debits", df)


def test_provider_payment_typing_refunds_and_subscription_from_notes() -> None:
    df = silver.provider_payments(T, [
        {"id": "pay_a", "_provider": "razorpay", "amount": 1000, "currency": "INR", "status": "captured",
         "amount_refunded": 1000, "created_at": 1790000000, "notes": {"subscription_id": "sub_9"}},
        {"id": "pay_b", "_provider": "razorpay", "amount": 1000, "currency": "INR", "status": "failed",
         "error_code": "BAD_REQUEST_ERROR", "error_description": "insufficient_funds", "created_at": 1790000100},
    ])
    assert list(df.status) == ["refunded", "failed"]
    assert df.subscription_id[0] == "sub_9" and df.error_reason[1] == "insufficient_funds"
    assert enforce("silver.provider_payments", df).quarantined.empty


# ------------------------------------------------------------------ gold cycles
def _pp(pid: str, status: str, at: datetime, *, invoice: str | None = None, sub: str | None = "sub_1",
        reason: str | None = None) -> dict[str, Any]:
    return {"tenant_id": T, "provider": "razorpay", "payment_id": pid, "amount_minor": 99900, "currency": "INR",
            "status": status, "method": "upi", "bank": None,
            "error_code": "BAD_REQUEST_ERROR" if status == "failed" else None,
            "error_reason": reason, "invoice_id": invoice, "subscription_id": sub, "customer_id": "cust_1",
            "created_at": pd.Timestamp(at)}


def test_cycles_from_invoices_and_ungrouped_subscription_payments() -> None:
    lake = MemLake()
    d0 = NOW - timedelta(days=90)
    lake.tables["silver.provider_payments"] = pd.DataFrame([
        _pp("p1", "failed", d0, invoice="inv_1", reason="insufficient_funds"),
        _pp("p2", "captured", d0 + timedelta(days=3), invoice="inv_1"),
        _pp("p3", "captured", d0 + timedelta(days=30), invoice="inv_2"),
        # no invoice: failed, failed, captured within the gap → ONE recovered cycle
        _pp("p4", "failed", d0 + timedelta(days=60), sub="sub_2", reason="mandate_revoked"),
        _pp("p5", "failed", d0 + timedelta(days=61), sub="sub_2"),
        _pp("p6", "captured", d0 + timedelta(days=62), sub="sub_2"),
        # recent failure, not paid yet → open (label not final)
        _pp("p7", "failed", NOW - timedelta(days=2), sub="sub_3"),
    ])
    lake.tables["silver.provider_invoices"] = pd.DataFrame([
        {"tenant_id": T, "provider": "razorpay", "invoice_id": i, "subscription_id": "sub_1", "payment_id": p,
         "status": "paid", "amount_minor": 99900, "billing_start": pd.Timestamp(d), "billing_end": None,
         "invoice_date": pd.Timestamp(d), "paid_at": None, "created_at": pd.Timestamp(d)}
        for i, p, d in (("inv_1", "p2", d0), ("inv_2", "p3", d0 + timedelta(days=30)))])
    res = gold.build_charge_outcomes(lake, T, now=NOW)  # type: ignore[arg-type]
    df = lake.tables[gold.TABLE].set_index("cycle_id")
    assert res.by_source == {"invoice": 2, "subscription_payments": 2}
    c1 = df.loc["inv:inv_1"]
    assert c1.first_attempt_failed and c1.recovered and c1.attempts == 2 and c1.days_to_recover == 3.0
    assert c1.failure_category == "INSUFFICIENT_FUNDS"
    assert not df.loc["inv:inv_2"].first_attempt_failed and not df.loc["inv:inv_2"].recovered
    grouped = df.loc["sp:p4"]
    assert grouped.attempts == 3 and grouped.recovered and grouped.failure_category == "MANDATE_REVOKED"
    open_cycle = df.loc["sp:p7"]
    assert open_cycle.first_attempt_failed and not open_cycle.paid and not open_cycle.label_final
    assert gold.invariant_violations(lake.tables[gold.TABLE]) == {}


def test_invariants_catch_inconsistent_labels() -> None:
    bad = pd.DataFrame([{"cycle_id": "x", "recovered": True, "first_attempt_failed": False, "paid": True,
                         "paid_at": None, "days_to_recover": -1.0, "amount_minor": 0, "attempts": 1}] * 2)
    v = gold.invariant_violations(bad)
    assert set(v) == {"duplicate_cycle_id", "recovered_without_failure_or_payment", "paid_without_paid_at",
                      "negative_days_to_recover", "non_positive_amount"}


def test_backfill_windows_cover_range_exactly() -> None:
    w = list(_windows(NOW - timedelta(days=20), NOW, timedelta(days=7)))
    assert w[0][0] == NOW - timedelta(days=20) and w[-1][1] == NOW and len(w) == 3
    assert all(a[1] == b[0] for a, b in pairwise(w))


def test_recovery_rate_uses_a_matured_cohort_not_settled_cycles() -> None:
    """Censoring: recent recovered failures settle immediately, recent unrecovered ones only after 30 days.
    Counting 'settled' failures would inflate the recovery rate; only failures older than the horizon count."""
    from nirantar.data import health

    lake = MemLake()
    old, new = NOW - timedelta(days=60), NOW - timedelta(days=5)
    rows = [_pp(f"o{i}", "failed", old, sub=f"so{i}") for i in range(4)]                   # 4 old failures…
    rows += [_pp("o0r", "captured", old + timedelta(days=1), sub="so0")]                     # …1 recovered
    rows += [_pp(f"n{i}", "failed", new, sub=f"sn{i}") for i in range(4)]                   # 4 recent failures…
    rows += [_pp(f"n{i}r", "captured", new + timedelta(days=1), sub=f"sn{i}") for i in range(2)]  # …2 recovered
    lake.tables["silver.provider_payments"] = pd.DataFrame(rows)
    gold.build_charge_outcomes(lake, T, now=NOW)  # type: ignore[arg-type]
    assert health.compute(lake, T, now=NOW)["labels"]["recovery_rate"] is None  # type: ignore[arg-type]  # 4 < 30
    rep = health.compute(lake, T, now=NOW, min_rate_cohort=1)  # type: ignore[arg-type]
    labels = rep["labels"]
    assert labels["first_attempt_failures"] == 8 and labels["failure_rate"] == 1.0
    assert labels["recovery_cohort"] == 4 and labels["recovery_rate"] == 0.25   # 1/4, not 3/5 "settled"
    assert labels["failures_awaiting_outcome"] == 2
