"""Tenant-scoped database access (BB-§7).

The application role (nirantar_app) is subject to row-level security. Every
transaction sets `app.tenant_id` with SET LOCAL semantics, so the database only
returns and accepts rows for that tenant. There is no code path that queries
tenant tables without a tenant.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from nirantar.core.tenancy import tenant_scope, validate_tenant_id

DEFAULT_APP_URL = "postgresql+psycopg://nirantar_app:nirantar_app@localhost:25432/nirantar"

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ.get("DATABASE_URL", DEFAULT_APP_URL), pool_pre_ping=True, pool_size=10
        )
    return _engine


@contextmanager
def tenant_tx(tenant_id: str, engine: Engine | None = None) -> Iterator[Connection]:
    """One transaction, bound to one tenant, enforced by Postgres RLS."""
    validate_tenant_id(tenant_id)
    eng = engine or get_engine()
    with tenant_scope(tenant_id), eng.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield conn
