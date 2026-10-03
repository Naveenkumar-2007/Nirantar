"""Bronze layer: land raw data, append-only, PII redacted, with lineage (run_id, ingested_at, content hash).

Two feeds:
1. Provider backfill (`backfill`) — the tenant's history from its payment provider, in time windows. A window
   is checkpointed only after its rows are in the lake, and only if it ended at least `settle` ago (recent
   windows keep changing, so they are re-read every run; silver deduplicates). Re-running is always safe.
2. Nirantar change capture (`extract_changes`) — rows changed in Nirantar's own tables since the last
   watermark, using transaction ids (migration 0010): rows from transactions still running at extraction time
   are re-read next run, so no committed change is ever skipped.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
from sqlalchemy import Engine, text

from nirantar.data.lake import Lake
from nirantar.data.pii import redact
from nirantar.data.sources import ENTITIES, BackfillSource
from nirantar.db.session import tenant_tx
from nirantar.payments.domain import ProviderAuthError

PROVIDER_TABLE = "bronze.provider_records"
CHANGES_TABLE = "bronze.nirantar_changes"

PROVIDER_SCHEMA = pa.schema([
    ("tenant_id", pa.string()), ("provider", pa.string()), ("entity", pa.string()), ("entity_id", pa.string()),
    ("entity_created_at", pa.timestamp("us", tz="UTC")), ("payload", pa.string()), ("payload_sha256", pa.string()),
    ("ingested_at", pa.timestamp("us", tz="UTC")), ("run_id", pa.string()),
])
CHANGES_SCHEMA = pa.schema([
    ("tenant_id", pa.string()), ("source_table", pa.string()), ("pk", pa.string()), ("row", pa.string()),
    ("cdc_txid", pa.int64()), ("ingested_at", pa.timestamp("us", tz="UTC")), ("run_id", pa.string()),
])

# table → primary key columns (tenant_id implied). Columns holding raw personal data are never selected.
CDC_SOURCES: dict[str, tuple[str, ...]] = {
    "billing.customers": ("customer_id",), "billing.subscriptions": ("subscription_id",),
    "billing.debits": ("debit_id",), "billing.payments": ("payment_id",), "billing.mandates": ("mandate_id",),
    "ops.contacts": ("contact_id",), "ops.cases": ("case_id",),
    "experiments.assignments": ("experiment_id", "customer_id"), "experiments.exposures": ("exposure_id",),
    "experiments.outcomes": ("outcome_id",), "ai.predictions": ("prediction_id",), "ai.labels": ("label_id",),
    "events.outbox": ("event_id",),
}
EXCLUDED_COLUMNS = frozenset({"display_name", "phone_enc", "email_enc", "cdc_txid"})


@dataclass(frozen=True)
class BronzeResult:
    feed: str
    rows: int
    detail: dict[str, Any]


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ts(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v), UTC)
    return datetime.fromisoformat(str(v))


# ------------------------------------------------------------------ provider backfill
def _windows(since: datetime, until: datetime, size: timedelta) -> Iterator[tuple[datetime, datetime]]:
    start = since
    while start < until:
        end = min(start + size, until)
        yield start, end
        start = end


def _done_windows(engine: Engine, tenant_id: str, provider: str, entity: str) -> set[tuple[datetime, datetime]]:
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text("SELECT window_start, window_end FROM ingest.backfill_checkpoints WHERE tenant_id=:t "
                              "AND provider=:p AND entity=:e"), {"t": tenant_id, "p": provider, "e": entity}).all()
    return {(r.window_start, r.window_end) for r in rows}


def _land(lake: Lake, tenant_id: str, provider: str, entity: str, records: list[dict[str, Any]], run_id: str,
          now: datetime) -> int:
    rows = []
    for rec in records:
        clean = json.dumps(redact(rec, tenant_id), sort_keys=True, default=str)
        rows.append({"tenant_id": tenant_id, "provider": provider, "entity": entity, "entity_id": str(rec["id"]),
                     "entity_created_at": _ts(rec.get("created_at")), "payload": clean, "payload_sha256": _sha(clean),
                     "ingested_at": now, "run_id": run_id})
    return lake.append(PROVIDER_TABLE, tenant_id, pa.Table.from_pylist(rows, schema=PROVIDER_SCHEMA))


def backfill(engine: Engine, lake: Lake, tenant_id: str, source: BackfillSource, *, since: datetime,
             until: datetime, run_id: str, now: datetime, window: timedelta = timedelta(days=7),
             settle: timedelta = timedelta(days=2)) -> BronzeResult:
    detail: dict[str, Any] = {}
    total = 0
    denied: list[str] = []
    for entity in ENTITIES:
        done = _done_windows(engine, tenant_id, source.provider, entity)
        fetched = skipped = 0
        try:
            first = next(iter(_windows(since, until, window)))
            list(source.list(entity, first[0], first[0]))      # zero-width probe: is this product enabled?
        except ProviderAuthError as exc:
            # One product not activated for this account (observed: Razorpay test account → subscriptions 401).
            # Keys that are wrong fail every entity, which is raised below.
            denied.append(entity)
            detail[entity] = {"rows": 0, "status": "not_authorized", "error": str(exc)[:200]}
            continue
        for ws, we in _windows(since, until, window):
            if (ws, we) in done:
                skipped += 1
                continue
            records = list(source.list(entity, ws, we))
            n = _land(lake, tenant_id, source.provider, entity, records, run_id, now)
            fetched += n
            if we <= now - settle:            # only settled windows are never re-read
                with tenant_tx(tenant_id, engine) as c:
                    c.execute(text("INSERT INTO ingest.backfill_checkpoints (tenant_id, provider, entity, "
                                   "window_start, window_end, rows, completed_at) VALUES (:t, :p, :e, :s, :w, :n, :at) "
                                   "ON CONFLICT DO NOTHING"),
                              {"t": tenant_id, "p": source.provider, "e": entity, "s": ws, "w": we, "n": n,
                               "at": now})
        detail[entity] = {"rows": fetched, "windows_skipped": skipped, "status": "ok"}
        total += fetched
    if len(denied) == len(ENTITIES):
        raise ProviderAuthError(f"{source.provider}: credentials rejected for every entity ({', '.join(denied)})")
    # Invoices link payments to subscription billing cycles; fetched per subscription seen in bronze.
    subs = lake.read(PROVIDER_TABLE, tenant_id, ("entity", "entity_id", "provider"))
    sub_ids = sorted(set(subs.loc[(subs.entity == "subscriptions") & (subs.provider == source.provider),
                                  "entity_id"])) if not subs.empty else []
    inv = 0
    for sid in sub_ids:
        inv += _land(lake, tenant_id, source.provider, "invoices", list(source.invoices_for(sid)), run_id, now)
    detail["invoices"] = {"rows": inv, "subscriptions": len(sub_ids)}
    return BronzeResult("provider_backfill", total + inv, detail)


# ------------------------------------------------------------------ Nirantar change capture
def extract_changes(engine: Engine, lake: Lake, tenant_id: str, *, run_id: str, now: datetime) -> BronzeResult:
    detail: dict[str, int] = {}
    total = 0
    for table, pk in CDC_SOURCES.items():
        with tenant_tx(tenant_id, engine) as c:
            wm = c.execute(text("SELECT watermark::text FROM ingest.extract_watermarks WHERE tenant_id=:t "
                                "AND source_table=:s"), {"t": tenant_id, "s": table}).scalar_one_or_none()
            horizon: str = c.execute(text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text")).scalar_one()
            where = "cdc_txid >= CAST(:wm AS xid8)" if wm else "true"
            rows = c.execute(text(f"SELECT *, cdc_txid::text AS _txid FROM {table} WHERE tenant_id=:t AND "  # noqa: S608
                                  f"{where}"), {"t": tenant_id, "wm": wm}).mappings().all()
        out = []
        for r in rows:
            clean = {k: v for k, v in r.items() if k not in EXCLUDED_COLUMNS and k != "_txid"
                     and not isinstance(v, (bytes, memoryview))}
            out.append({"tenant_id": tenant_id, "source_table": table,
                        "pk": "|".join(str(r[k]) for k in pk),
                        "row": json.dumps(redact(clean, tenant_id), sort_keys=True, default=str),
                        "cdc_txid": int(r["_txid"]) if r["_txid"] else 0, "ingested_at": now, "run_id": run_id})
        n = lake.append(CHANGES_TABLE, tenant_id, pa.Table.from_pylist(out, schema=CHANGES_SCHEMA))
        with tenant_tx(tenant_id, engine) as c:     # advance only after the rows are safely in the lake
            c.execute(text("INSERT INTO ingest.extract_watermarks (tenant_id, source_table, watermark, rows_total, "
                           "updated_at) VALUES (:t, :s, CAST(:w AS xid8), :n, :at) "
                           "ON CONFLICT (tenant_id, source_table) "
                           "DO UPDATE SET watermark=EXCLUDED.watermark, "
                           "rows_total=ingest.extract_watermarks.rows_total + EXCLUDED.rows_total, "
                           "updated_at=EXCLUDED.updated_at"),
                      {"t": tenant_id, "s": table, "w": horizon, "n": n, "at": now})
        detail[table] = n
        total += n
    return BronzeResult("nirantar_changes", total, detail)
