"""Lakehouse access: Apache Iceberg tables (PyIceberg) on S3-compatible storage, catalog in Postgres.

Layers are Iceberg namespaces: bronze (raw, append-only, PII redacted at landing), silver (typed, deduplicated,
contract-checked), gold (training/analytics-ready). Every table carries `tenant_id` and is partitioned by it.
Tenant isolation: there is no API to read or overwrite without naming a tenant — reads always filter on
tenant_id and writes replace only that tenant's partition.

Config (env): NIRANTAR_LAKE_CATALOG_URI, NIRANTAR_LAKE_WAREHOUSE, NIRANTAR_S3_ENDPOINT, NIRANTAR_S3_ACCESS_KEY,
NIRANTAR_S3_SECRET_KEY. Defaults match docker-compose (SeaweedFS on :8333, catalog DB `nirantar_lake`).
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import EqualTo, Reference
from pyiceberg.expressions.literals import literal
from pyiceberg.table import Table
from pyiceberg.transforms import IdentityTransform

from nirantar.core.tenancy import validate_tenant_id

LAYERS = ("bronze", "silver", "gold")


def default_catalog() -> Catalog:
    return SqlCatalog("nirantar", **{
        "uri": os.environ.get("NIRANTAR_LAKE_CATALOG_URI",
                              "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar_lake"),
        "warehouse": os.environ.get("NIRANTAR_LAKE_WAREHOUSE", "s3://nirantar-lake/warehouse"),
        "s3.endpoint": os.environ.get("NIRANTAR_S3_ENDPOINT", "http://localhost:8333"),
        "s3.access-key-id": os.environ.get("NIRANTAR_S3_ACCESS_KEY", "nirantar"),
        "s3.secret-access-key": os.environ.get("NIRANTAR_S3_SECRET_KEY", "nirantar-secret"),
        "s3.region": os.environ.get("NIRANTAR_S3_REGION", "us-east-1"),
        "s3.force-virtual-addressing": "false",
    })


def ensure_bucket() -> None:
    """Create the warehouse bucket on first use (S3-compatible stores only)."""
    import boto3

    wh = os.environ.get("NIRANTAR_LAKE_WAREHOUSE", "s3://nirantar-lake/warehouse")
    if not wh.startswith("s3://"):
        return
    bucket = wh[5:].split("/", 1)[0]
    s3 = boto3.client("s3", endpoint_url=os.environ.get("NIRANTAR_S3_ENDPOINT", "http://localhost:8333"),
                      aws_access_key_id=os.environ.get("NIRANTAR_S3_ACCESS_KEY", "nirantar"),
                      aws_secret_access_key=os.environ.get("NIRANTAR_S3_SECRET_KEY", "nirantar-secret"),
                      region_name=os.environ.get("NIRANTAR_S3_REGION", "us-east-1"))
    if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket=bucket)


@dataclass
class Lake:
    catalog: Catalog

    @classmethod
    def from_env(cls) -> Lake:
        ensure_bucket()
        return cls(default_catalog())

    def _table(self, name: str, schema: pa.Schema | None = None) -> Table:
        layer = name.split(".", 1)[0]
        if layer not in LAYERS:
            raise ValueError(f"table {name!r} must be in one of {LAYERS}")
        try:
            tbl = self.catalog.load_table(name)
        except NoSuchTableError:
            if schema is None:
                raise
            if "tenant_id" not in schema.names:
                raise ValueError(f"{name}: every lake table needs a tenant_id column") from None
            self.catalog.create_namespace_if_not_exists(layer)
            tbl = self.catalog.create_table(name, schema=schema)
            with tbl.update_spec() as spec:                  # identity partition per tenant
                spec.add_field("tenant_id", IdentityTransform(), "tenant_id")
            return self.catalog.load_table(name)
        if schema is not None:
            missing = [f for f in schema if f.name not in tbl.schema().column_names]
            removed = [c for c in tbl.schema().column_names if c not in schema.names]
            if removed:
                raise ValueError(f"{name}: columns {removed} missing from the new data (only additive changes)")
            if missing:                                  # additive schema evolution (new nullable columns)
                with tbl.update_schema() as upd:
                    upd.union_by_name(pa.schema(missing))
                tbl = self.catalog.load_table(name)
        return tbl

    def exists(self, name: str) -> bool:
        try:
            self.catalog.load_table(name)
            return True
        except NoSuchTableError:
            return False

    def append(self, name: str, tenant_id: str, data: pa.Table) -> int:
        """Append rows for ONE tenant (bronze). Refuses rows belonging to any other tenant."""
        validate_tenant_id(tenant_id)
        if data.num_rows == 0:
            return 0
        _only_tenant(data, tenant_id)
        tbl = self._table(name, data.schema)
        tbl.append(_conform(data, tbl))
        return int(data.num_rows)

    def replace_tenant(self, name: str, tenant_id: str, data: pa.Table) -> int:
        """Atomically replace one tenant's partition (silver/gold rebuilds). Other tenants are untouched."""
        validate_tenant_id(tenant_id)
        _only_tenant(data, tenant_id)
        tbl = self._table(name, data.schema)
        with warnings.catch_warnings():   # first write of a tenant: nothing to delete yet, which is fine
            warnings.filterwarnings("ignore", message="Delete operation did not match any records")
            tbl.overwrite(_conform(data, tbl), overwrite_filter=_tenant_filter(tenant_id))
        rows: int = data.num_rows
        return rows

    def read(self, name: str, tenant_id: str, columns: tuple[str, ...] | None = None) -> pd.DataFrame:
        validate_tenant_id(tenant_id)
        try:
            tbl = self.catalog.load_table(name)
        except NoSuchTableError:
            return pd.DataFrame()
        scan = tbl.scan(row_filter=_tenant_filter(tenant_id),
                        selected_fields=columns if columns else ("*",))
        df: pd.DataFrame = scan.to_arrow().to_pandas()
        return df

    def snapshot_info(self, name: str) -> dict[str, Any] | None:
        try:
            snap = self.catalog.load_table(name).current_snapshot()
        except NoSuchTableError:
            return None
        return None if snap is None else {"snapshot_id": snap.snapshot_id, "timestamp_ms": snap.timestamp_ms}


def _conform(data: pa.Table, tbl: Table) -> pa.Table:
    """Order columns by name as the table stores them (evolution appends new columns last), then cast."""
    target = tbl.schema().as_arrow()
    return data.select(target.names).cast(target)


def _tenant_filter(tenant_id: str) -> EqualTo:
    return EqualTo(term=Reference("tenant_id"), value=literal(tenant_id))


def _only_tenant(data: pa.Table, tenant_id: str) -> None:
    if "tenant_id" not in data.column_names:
        raise ValueError("rows must carry tenant_id")
    tenants = set(data.column("tenant_id").to_pylist())
    if tenants and tenants != {tenant_id}:
        raise ValueError(f"refusing to write rows for other tenants: {sorted(tenants - {tenant_id})}")
