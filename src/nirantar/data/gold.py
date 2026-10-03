"""Gold layer: `gold.charge_outcomes` — one row per billing cycle, the label table for M1/M3/M7 (P3).

A cycle = one amount due for one subscription period, with every payment attempt against it. Sources, in order
of fidelity (each attempt is used by at most one cycle):
  1. provider invoices (Razorpay: invoice ↔ subscription ↔ payments, billing_start = due)
  2. Nirantar debits (+ their payments), when not already covered by an invoice
  3. provider subscription payments without an invoice, grouped: a cycle starts at an attempt and collects
     later attempts until one is captured or `cycle_gap` passes.
Labels are only "final" once a cycle is paid or older than `recovery_horizon` — open cycles are kept but
flagged, so training never treats "not paid yet" as "never paid".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pyarrow as pa

from nirantar.agents.failure_triage import TriageIn, triage
from nirantar.data.lake import Lake

TABLE = "gold.charge_outcomes"
RECOVERY_HORIZON = timedelta(days=30)     # after this, an unpaid failed cycle counts as unrecovered
SCHEMA = pa.schema([
    ("tenant_id", pa.string()), ("cycle_id", pa.string()), ("source", pa.string()), ("subscription_id", pa.string()),
    ("customer_id", pa.string()), ("debit_id", pa.string()), ("invoice_id", pa.string()),
    ("due_at", pa.timestamp("us", tz="UTC")), ("amount_minor", pa.int64()), ("attempts", pa.int64()),
    ("first_attempt_at", pa.timestamp("us", tz="UTC")), ("first_attempt_failed", pa.bool_()),
    ("first_error_code", pa.string()), ("first_error_reason", pa.string()), ("failure_category", pa.string()),
    ("method", pa.string()), ("bank", pa.string()),
    ("paid", pa.bool_()), ("paid_at", pa.timestamp("us", tz="UTC")), ("recovered", pa.bool_()),
    ("days_to_recover", pa.float64()), ("label_final", pa.bool_()), ("as_of", pa.timestamp("us", tz="UTC")),
])


@dataclass(frozen=True)
class GoldResult:
    rows: int
    by_source: dict[str, int]
    final_labels: int
    failures: int


def _none(v: Any) -> Any:
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT else v


def _cycle(tenant_id: str, cycle_id: str, source: str, attempts: pd.DataFrame, *, subscription_id: Any,
           customer_id: Any, debit_id: Any, invoice_id: Any, due_at: Any, amount: Any, now: datetime,
           horizon: timedelta) -> dict[str, Any]:
    attempts = attempts.sort_values("ts")
    first = attempts.iloc[0] if len(attempts) else None
    captured = attempts[attempts.status.isin(["captured", "refunded", "partially_refunded"])]
    paid_at = captured.ts.min() if len(captured) else None
    first_failed = None if first is None else bool(first.status == "failed")
    cat = None
    if first is not None and first_failed:
        cat = triage(TriageIn(error_code=_none(first.error_code), error_reason=_none(first.error_reason)),
                     llm=None).category
    recovered = bool(first_failed and paid_at is not None)
    start = first.ts if first is not None else due_at
    final = paid_at is not None or (start is not None and not pd.isna(start) and now - start >= horizon)
    return {
        "tenant_id": tenant_id, "cycle_id": cycle_id, "source": source, "subscription_id": _none(subscription_id),
        "customer_id": _none(customer_id), "debit_id": _none(debit_id), "invoice_id": _none(invoice_id),
        "due_at": _none(due_at) if due_at is not None else (None if first is None else first.ts),
        "amount_minor": int(amount) if _none(amount) is not None else None, "attempts": len(attempts),
        "first_attempt_at": None if first is None else first.ts, "first_attempt_failed": first_failed,
        "first_error_code": None if first is None else _none(first.error_code),
        "first_error_reason": None if first is None else _none(first.error_reason), "failure_category": cat,
        "method": None if first is None else _none(first.get("method")),
        "bank": None if first is None else _none(first.get("bank")),
        "paid": paid_at is not None, "paid_at": paid_at, "recovered": recovered,
        "days_to_recover": ((paid_at - first.ts).total_seconds() / 86400) if recovered and first is not None else None,
        "label_final": bool(final), "as_of": now,
    }


def build_charge_outcomes(lake: Lake, tenant_id: str, *, now: datetime,
                          recovery_horizon: timedelta = RECOVERY_HORIZON,
                          cycle_gap: timedelta = timedelta(days=10)) -> GoldResult:
    pp = lake.read("silver.provider_payments", tenant_id)
    inv = lake.read("silver.provider_invoices", tenant_id)
    debits = lake.read("silver.debits", tenant_id)
    npay = lake.read("silver.payments", tenant_id)
    rows: list[dict[str, Any]] = []
    used: set[str] = set()                       # provider payment ids already assigned to a cycle

    def attempts_from(df: pd.DataFrame, id_col: str, at_col: str) -> pd.DataFrame:
        cols = ["pid", "ts", "status", "error_code", "error_reason", "method", "bank"]
        if df.empty:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame({"pid": df[id_col], "ts": df[at_col], "status": df["status"],
                             "error_code": df["error_code"], "error_reason": df["error_reason"],
                             "method": df["method"] if "method" in df else None,
                             "bank": df["bank"] if "bank" in df else None})

    # 1. invoices
    if not inv.empty:
        by_inv = pp.groupby("invoice_id") if not pp.empty else None
        for r in inv.itertuples():
            att = attempts_from(by_inv.get_group(r.invoice_id), "payment_id", "created_at") \
                if by_inv is not None and r.invoice_id in by_inv.groups else attempts_from(pd.DataFrame(), "", "")
            if att.empty and _none(r.payment_id) and not pp.empty:
                att = attempts_from(pp[pp.payment_id == r.payment_id], "payment_id", "created_at")
            used |= set(att.pid)
            rows.append(_cycle(tenant_id, f"inv:{r.invoice_id}", "invoice", att, subscription_id=r.subscription_id,
                               customer_id=None, debit_id=None, invoice_id=r.invoice_id,
                               due_at=_none(r.billing_start) or _none(r.invoice_date) or r.created_at,
                               amount=r.amount_minor, now=now, horizon=recovery_horizon))
    # 2. Nirantar debits (open ones — not yet attempted — are skipped)
    if not debits.empty:
        pay_by_debit = npay.groupby("debit_id") if not npay.empty else None
        for d in debits.itertuples():
            if d.status in ("scheduled", "notified", "cancelled"):
                continue
            src = pay_by_debit.get_group(d.debit_id) if pay_by_debit is not None and d.debit_id in pay_by_debit.groups \
                else pd.DataFrame()
            if not src.empty:
                src = src.assign(ts=src.provider_created_at.fillna(src.created_at))
            att = attempts_from(src, "provider_payment_id", "ts") if not src.empty else attempts_from(src, "", "")
            if used & set(att.pid):
                continue                          # the provider invoice already describes this cycle
            used |= set(att.pid)
            rows.append(_cycle(tenant_id, f"deb:{d.debit_id}", "debit", att, subscription_id=d.subscription_id,
                               customer_id=d.customer_id, debit_id=d.debit_id, invoice_id=None,
                               due_at=d.scheduled_for, amount=d.amount_minor, now=now, horizon=recovery_horizon))
    # 3. subscription payments with no invoice and no debit
    if not pp.empty:
        rest = pp[pp.subscription_id.notna() & ~pp.payment_id.isin(used)].sort_values("created_at")
        for sub_id, g in rest.groupby("subscription_id"):
            current: list[Any] = []
            for p in g.itertuples():
                if current and (p.created_at - current[0].created_at > cycle_gap
                                or current[-1].status == "captured"):
                    rows.append(_group_cycle(tenant_id, sub_id, current, now, recovery_horizon))
                    current = []
                current.append(p)
            if current:
                rows.append(_group_cycle(tenant_id, sub_id, current, now, recovery_horizon))
    df = pd.DataFrame(rows, columns=SCHEMA.names)
    lake.replace_tenant(TABLE, tenant_id, pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False))
    labelled = df[df.first_attempt_failed.notna()] if not df.empty else df
    by_source = {str(k): int(v) for k, v in df.source.value_counts().items()} if not df.empty else {}
    return GoldResult(len(df), by_source,
                      int(df.label_final.sum()) if not df.empty else 0,
                      int(labelled.first_attempt_failed.astype(bool).sum()) if not labelled.empty else 0)


def _group_cycle(tenant_id: str, sub_id: Any, ps: list[Any], now: datetime, horizon: timedelta) -> dict[str, Any]:
    att = pd.DataFrame({"pid": [p.payment_id for p in ps], "ts": [p.created_at for p in ps],
                        "status": [p.status for p in ps], "error_code": [p.error_code for p in ps],
                        "error_reason": [p.error_reason for p in ps], "method": [p.method for p in ps],
                        "bank": [p.bank for p in ps]})
    return _cycle(tenant_id, f"sp:{ps[0].payment_id}", "subscription_payments", att, subscription_id=sub_id,
                  customer_id=ps[0].customer_id, debit_id=None, invoice_id=None, due_at=ps[0].created_at,
                  amount=ps[0].amount_minor, now=now, horizon=horizon)


def invariant_violations(df: pd.DataFrame) -> dict[str, int]:
    """Rules the label table must always satisfy; any count > 0 fails the Dagster asset check."""
    if df.empty:
        return {}
    ffail = df.first_attempt_failed
    checks = {
        "duplicate_cycle_id": int(df.cycle_id.duplicated().sum()),
        "recovered_without_failure_or_payment":
            int((df.recovered & ~(ffail.fillna(False).astype(bool) & df.paid)).sum()),
        "paid_without_paid_at": int((df.paid & df.paid_at.isna()).sum()),
        "negative_days_to_recover": int((df.days_to_recover.dropna() < 0).sum()),
        "non_positive_amount": int((df.amount_minor.dropna() <= 0).sum()),
        "attempt_status_without_attempts": int((ffail.notna() & (df.attempts == 0)).sum()),
    }
    return {k: v for k, v in checks.items() if v}
