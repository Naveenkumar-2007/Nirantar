"""Silver layer: typed, deduplicated, contract-checked tables rebuilt per tenant from bronze.

- Provider records: latest version of each (entity, id) by ingestion time. Provider data is truth (invariant 5).
- Nirantar changes: latest version of each (table, pk) by transaction id.
- Types are converted here (bad values become nulls) so contract failures are attributable to rows;
  failing rows go to `silver.quarantine` with the reason, never silently into training data.
A rebuild is a pure function of bronze, so it can be re-run at any time (replace-partition write).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
import pyarrow as pa

from nirantar.data.bronze import CHANGES_TABLE, PROVIDER_TABLE
from nirantar.data.contracts import CONTRACTS, enforce
from nirantar.data.lake import Lake

QUARANTINE = "silver.quarantine"
QUARANTINE_SCHEMA = pa.schema([("tenant_id", pa.string()), ("table_name", pa.string()), ("reason", pa.string()),
                               ("row", pa.string()), ("run_id", pa.string()),
                               ("quarantined_at", pa.timestamp("us", tz="UTC"))])


@dataclass
class SilverResult:
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return sum(t["rows"] for t in self.tables.values())

    @property
    def quarantined(self) -> int:
        return sum(t["quarantined"] for t in self.tables.values())


# ------------------------------------------------------------------ helpers
def _ts_unix(s: pd.Series) -> pd.Series:
    return pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="s", utc=True).astype("datetime64[us, UTC]")


def _ts_iso(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce", format="mixed").astype("datetime64[us, UTC]")


def _int(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def _str(s: pd.Series) -> pd.Series:
    return s.where(s.notna(), None).map(lambda v: None if v is None or v == "" else str(v))


def _latest_provider(lake: Lake, tenant_id: str, entity: str) -> list[dict[str, Any]]:
    df = lake.read(PROVIDER_TABLE, tenant_id)
    if df.empty:
        return []
    df = df[df.entity == entity].sort_values(["ingested_at", "payload_sha256"])
    df = df.drop_duplicates(subset=["provider", "entity_id"], keep="last")
    return [{**json.loads(p), "_provider": prov} for p, prov in zip(df.payload, df.provider, strict=True)]


def _latest_changes(lake: Lake, tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    df = lake.read(CHANGES_TABLE, tenant_id)
    if df.empty:
        return {}
    df = df.sort_values(["cdc_txid", "ingested_at"]).drop_duplicates(subset=["source_table", "pk"], keep="last")
    return {str(t): [json.loads(r) for r in g.row] for t, g in df.groupby("source_table")}


def _frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows).reindex(columns=columns) if rows else pd.DataFrame(columns=columns)


# ------------------------------------------------------------------ builders (bronze → typed frames)
def provider_payments(tenant_id: str, recs: list[dict[str, Any]]) -> pd.DataFrame:
    df = _frame(recs, ["id", "_provider", "amount", "currency", "status", "method", "bank", "error_code",
                       "error_reason", "error_description", "invoice_id", "notes", "subscription_id", "customer_id",
                       "created_at", "amount_refunded"])
    notes = df["notes"].map(lambda n: n if isinstance(n, dict) else {})
    status = df["status"].astype("object")
    refunded = _int(df["amount_refunded"]).fillna(0)
    amount = _int(df["amount"])
    status = status.where(~((status == "captured") & (refunded > 0)),
                          (refunded >= amount).map({True: "refunded", False: "partially_refunded"}))
    return pd.DataFrame({
        "tenant_id": tenant_id, "provider": df["_provider"], "payment_id": df["id"], "amount_minor": amount,
        "currency": df["currency"].fillna("INR"), "status": status, "method": _str(df["method"]),
        "bank": _str(df["bank"]),
        "error_code": _str(df["error_code"]), "error_reason": _str(df["error_reason"].fillna(df["error_description"])),
        "invoice_id": _str(df["invoice_id"]),
        "subscription_id": _str(notes.map(lambda n: n.get("subscription_id")).fillna(df["subscription_id"])),
        "customer_id": _str(df["customer_id"]), "created_at": _ts_unix(df["created_at"]),
    })


def provider_subscriptions(tenant_id: str, recs: list[dict[str, Any]]) -> pd.DataFrame:
    df = _frame(recs, ["id", "_provider", "customer_id", "plan_id", "status", "created_at", "charge_at", "paid_count",
                       "ended_at", "total_count"])
    return pd.DataFrame({
        "tenant_id": tenant_id, "provider": df["_provider"], "subscription_id": df["id"],
        "customer_id": _str(df["customer_id"]), "plan_id": _str(df["plan_id"]), "status": df["status"],
        "created_at": _ts_unix(df["created_at"]), "charge_at": _ts_unix(df["charge_at"]),
        "paid_count": _int(df["paid_count"]).fillna(0), "ended_at": _ts_unix(df["ended_at"]),
        "total_count": _int(df["total_count"]),
    })


def provider_invoices(tenant_id: str, recs: list[dict[str, Any]]) -> pd.DataFrame:
    df = _frame(recs, ["id", "_provider", "subscription_id", "payment_id", "status", "amount", "billing_start",
                       "billing_end", "date", "paid_at", "created_at"])
    return pd.DataFrame({
        "tenant_id": tenant_id, "provider": df["_provider"], "invoice_id": df["id"],
        "subscription_id": _str(df["subscription_id"]), "payment_id": _str(df["payment_id"]), "status": df["status"],
        "amount_minor": _int(df["amount"]), "billing_start": _ts_unix(df["billing_start"]),
        "billing_end": _ts_unix(df["billing_end"]), "invoice_date": _ts_unix(df["date"]),
        "paid_at": _ts_unix(df["paid_at"]), "created_at": _ts_unix(df["created_at"]),
    })


def _typed(tenant_id: str, rows: list[dict[str, Any]], table: str) -> pd.DataFrame:
    """Nirantar rows → contract columns with explicit conversions by contract dtype."""
    schema = CONTRACTS[table]
    df = _frame(rows, list(schema.columns))
    out = {}
    for name, col in schema.columns.items():
        dtype = str(col.dtype)
        s = df[name]
        if name == "tenant_id":
            out[name] = pd.Series([tenant_id] * len(df), dtype="object")
        elif dtype.startswith("datetime64"):
            out[name] = _ts_iso(s)
        elif dtype == "int64":
            out[name] = _int(s)
        elif dtype == "float64":
            out[name] = pd.to_numeric(s, errors="coerce")
        elif dtype == "bool":
            out[name] = s.map(lambda v: v if isinstance(v, bool) else None)
        else:
            out[name] = s.map(lambda v: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list))
                              else (None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)))
    return pd.DataFrame(out)


NIRANTAR_TABLES = {
    "silver.debits": "billing.debits", "silver.payments": "billing.payments", "silver.customers": "billing.customers",
    "silver.subscriptions": "billing.subscriptions", "silver.contacts": "ops.contacts",
    "silver.outcomes": "experiments.outcomes", "silver.labels": "ai.labels", "silver.predictions": "ai.predictions",
}


# ------------------------------------------------------------------ build
_ARROW = {"int64": pa.int64(), "Int64": pa.int64(), "float64": pa.float64(), "bool": pa.bool_()}


def arrow_schema(table: str) -> pa.Schema:
    """Lake schema derived from the contract, so empty or all-null columns keep their real types."""
    fields = []
    for name, col in CONTRACTS[table].columns.items():
        dtype = str(col.dtype)
        typ = pa.timestamp("us", tz="UTC") if dtype.startswith("datetime64") else _ARROW.get(dtype, pa.string())
        fields.append(pa.field(name, typ, nullable=bool(col.nullable) or name != "tenant_id"))
    return pa.schema(fields)


def _write(lake: Lake, tenant_id: str, table: str, df: pd.DataFrame, run_id: str, now: datetime,
           result: SilverResult, quarantine: list[dict[str, Any]]) -> None:
    checked = enforce(table, df)
    lake.replace_tenant(table, tenant_id, pa.Table.from_pandas(checked.valid, schema=arrow_schema(table),
                                                               preserve_index=False))
    for _, row in checked.quarantined.iterrows():
        quarantine.append({"tenant_id": tenant_id, "table_name": table, "reason": row["reason"],
                           "row": json.dumps({k: v for k, v in row.items() if k != "reason"}, default=str),
                           "run_id": run_id, "quarantined_at": now})
    result.tables[table] = {"rows": len(checked.valid), "quarantined": len(checked.quarantined),
                            "failed_checks": checked.checks}


def build_silver(lake: Lake, tenant_id: str, *, run_id: str, now: datetime) -> SilverResult:
    result = SilverResult()
    quarantine: list[dict[str, Any]] = []
    for table, builder, entity in (("silver.provider_payments", provider_payments, "payments"),
                                   ("silver.provider_subscriptions", provider_subscriptions, "subscriptions"),
                                   ("silver.provider_invoices", provider_invoices, "invoices")):
        _write(lake, tenant_id, table, builder(tenant_id, _latest_provider(lake, tenant_id, entity)), run_id, now,
               result, quarantine)
    changes = _latest_changes(lake, tenant_id)
    for table, source in NIRANTAR_TABLES.items():
        _write(lake, tenant_id, table, _typed(tenant_id, changes.get(source, []), table), run_id, now, result,
               quarantine)
    lake.replace_tenant(QUARANTINE, tenant_id, pa.Table.from_pylist(quarantine, schema=QUARANTINE_SCHEMA))
    return result
