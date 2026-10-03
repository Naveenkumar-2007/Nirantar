"""Data contracts for silver tables (Pandera). A row that breaks a contract is quarantined with the reason;
a table-level break (missing column, wrong type) fails the run. Contracts are the promise that model training
(P3) never sees malformed data."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

PAYMENT_STATUS = ["created", "authorized", "captured", "failed", "refunded", "partially_refunded"]
SUB_STATUS = ["created", "authenticated", "active", "pending", "halted", "paused", "cancelled", "completed",
              "expired"]
DEBIT_STATUS = ["scheduled", "notified", "attempting", "succeeded", "failed", "cancelled"]
INVOICE_STATUS = ["draft", "issued", "partially_paid", "paid", "cancelled", "expired", "deleted"]


def _ts(nullable: bool = False) -> pa.Column:
    return pa.Column("datetime64[us, UTC]", nullable=nullable, coerce=True)


def _str(nullable: bool = False, isin: list[str] | None = None) -> pa.Column:
    return pa.Column(str, nullable=nullable, checks=[pa.Check.isin(isin)] if isin else None, coerce=True)


def _money() -> pa.Column:
    return pa.Column("int64", checks=pa.Check.ge(0), coerce=True)


TENANT = {"tenant_id": _str()}

CONTRACTS: dict[str, pa.DataFrameSchema] = {
    "silver.provider_payments": pa.DataFrameSchema({
        **TENANT, "provider": _str(), "payment_id": pa.Column(str, unique=True, coerce=True),
        "amount_minor": _money(), "currency": _str(), "status": _str(isin=PAYMENT_STATUS),
        "method": _str(True), "bank": _str(True), "error_code": _str(True), "error_reason": _str(True),
        "invoice_id": _str(True),
        "subscription_id": _str(True), "customer_id": _str(True), "created_at": _ts(),
    }, strict=True),
    "silver.provider_subscriptions": pa.DataFrameSchema({
        **TENANT, "provider": _str(), "subscription_id": pa.Column(str, unique=True, coerce=True),
        "customer_id": _str(True), "plan_id": _str(True), "status": _str(isin=SUB_STATUS), "created_at": _ts(),
        "charge_at": _ts(True), "paid_count": pa.Column("int64", pa.Check.ge(0), coerce=True),
        "ended_at": _ts(True), "total_count": pa.Column("Int64", pa.Check.ge(0), nullable=True, coerce=True),
    }, strict=True),
    "silver.provider_invoices": pa.DataFrameSchema({
        **TENANT, "provider": _str(), "invoice_id": pa.Column(str, unique=True, coerce=True),
        "subscription_id": _str(True), "payment_id": _str(True), "status": _str(isin=INVOICE_STATUS),
        "amount_minor": _money(), "billing_start": _ts(True), "billing_end": _ts(True), "invoice_date": _ts(True),
        "paid_at": _ts(True), "created_at": _ts(),
    }, strict=True),
    "silver.debits": pa.DataFrameSchema({
        **TENANT, "debit_id": pa.Column(str, unique=True, coerce=True), "subscription_id": _str(),
        "customer_id": _str(), "scheduled_for": _ts(), "amount_minor": pa.Column("int64", pa.Check.gt(0), coerce=True),
        "currency": _str(), "status": _str(isin=DEBIT_STATUS),
        "attempt_count": pa.Column("int64", pa.Check.ge(0), coerce=True), "provider_payment_id": _str(True),
        "last_error_code": _str(True), "created_at": _ts(), "updated_at": _ts(),
    }, strict=True),
    "silver.payments": pa.DataFrameSchema({
        **TENANT, "payment_id": pa.Column(str, unique=True, coerce=True), "provider": _str(),
        "provider_payment_id": _str(), "debit_id": _str(True), "customer_id": _str(True), "amount_minor": _money(),
        "status": _str(isin=PAYMENT_STATUS), "error_code": _str(True), "error_reason": _str(True),
        "provider_created_at": _ts(True), "created_at": _ts(),
    }, strict=True),
    "silver.customers": pa.DataFrameSchema({
        **TENANT, "customer_id": pa.Column(str, unique=True, coerce=True), "segment": _str(True),
        "preferred_language": _str(), "timezone": _str(), "contact_hash": _str(True), "consents": _str(),
        "created_at": _ts(),
    }, strict=True),
    "silver.subscriptions": pa.DataFrameSchema({
        **TENANT, "subscription_id": pa.Column(str, unique=True, coerce=True), "customer_id": _str(),
        "provider": _str(), "provider_subscription_id": _str(True), "amount_minor": _money(),
        "interval": _str(isin=["weekly", "monthly", "quarterly", "yearly"]),
        "status": _str(isin=["created", "active", "paused", "halted", "cancelled", "completed"]),
        "created_at": _ts(),
    }, strict=True),
    "silver.contacts": pa.DataFrameSchema({
        **TENANT, "contact_id": pa.Column(str, unique=True, coerce=True), "customer_id": _str(),
        "channel": _str(isin=["whatsapp", "sms", "email", "voice"]), "purpose": _str(), "status": _str(),
        "at": _ts(),
    }, strict=True),
    "silver.outcomes": pa.DataFrameSchema({
        **TENANT, "outcome_id": pa.Column(str, unique=True, coerce=True), "experiment_id": _str(),
        "customer_id": _str(), "debit_id": _str(True), "outcome": _str(), "value_minor": _money(),
        "verified": pa.Column(bool, coerce=True), "observed_at": _ts(),
    }, strict=True),
    "silver.labels": pa.DataFrameSchema({
        **TENANT, "label_id": pa.Column(str, unique=True, coerce=True), "prediction_id": _str(True),
        "subject_id": _str(), "label_name": _str(), "value": _str(), "source": _str(), "observed_at": _ts(),
    }, strict=True),
    "silver.predictions": pa.DataFrameSchema({
        **TENANT, "prediction_id": pa.Column(str, unique=True, coerce=True), "model_name": _str(),
        "model_version": _str(), "subject_id": _str(),
        "score": pa.Column(float, pa.Check.in_range(0, 1), nullable=True, coerce=True), "predicted_at": _ts(),
    }, strict=True),
}


class ContractError(ValueError):
    """Table-level break (columns missing/unexpected): the whole run fails, nothing is written."""


@dataclass(frozen=True)
class Checked:
    valid: pd.DataFrame
    quarantined: pd.DataFrame          # original rows + `reason`
    checks: dict[str, int]              # "column:check" → failing rows


def _row_failures(schema: pa.DataFrameSchema, df: pd.DataFrame) -> dict[int, str]:
    try:
        schema.validate(df, lazy=True)
        return {}
    except SchemaErrors as err:
        fc = err.failure_cases
        out: dict[int, set[str]] = {}
        unindexed = fc[fc["index"].isna()]
        for _, f in fc.dropna(subset=["index"]).iterrows():
            out.setdefault(int(f["index"]), set()).add(f"{f['column']}: {f['check']}")
        if not unindexed.empty:
            # Pandera could not attribute some failures to rows: find them one row at a time (rare path).
            for i in df.index:
                if i in out:
                    continue
                try:
                    schema.validate(df.loc[[i]], lazy=True)
                except SchemaErrors as e1:
                    out[int(i)] = {f"{r['column']}: {r['check']}" for _, r in e1.failure_cases.iterrows()
                                   if r["check"] != "field_uniqueness"}
                    if not out[int(i)]:
                        del out[int(i)]
        return {i: "; ".join(sorted(v)) for i, v in out.items()}


def enforce(table: str, df: pd.DataFrame) -> Checked:
    """Validate row by row: failing rows are quarantined with reasons; missing/unexpected columns raise."""
    schema = CONTRACTS[table]
    expected = list(schema.columns)
    if set(df.columns) != set(expected):
        raise ContractError(f"{table}: columns {sorted(df.columns)} != contract {sorted(expected)}")
    df = df[expected].reset_index(drop=True)
    failures = _row_failures(schema, df)
    idx = sorted(failures)
    quarantined = df.loc[idx].assign(reason=[failures[i] for i in idx])
    valid = schema.validate(df.drop(index=idx)) if len(idx) < len(df) else df.iloc[0:0]
    counts: dict[str, int] = {}
    for reason in failures.values():
        for part in reason.split("; "):
            counts[part] = counts.get(part, 0) + 1
    return Checked(valid.reset_index(drop=True), quarantined.reset_index(drop=True), counts)
