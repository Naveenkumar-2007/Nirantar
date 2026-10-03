"""A business's own credentials, encrypted at rest with its tenant data key (P8.2, ADR-0019).

Stored values are never returned by the API; only the reference (`tenant:<secret_id>`) is kept on the account row.
Replacing a credential retires the old secret instead of overwriting it, so the change is auditable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine, text

from nirantar.core import crypto
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx


def put_secret(engine: Engine, tenant_id: str, purpose: str, value: str, actor: str) -> str:
    sid = new_id("sec")
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("UPDATE core.tenant_secrets SET retired_at=:n WHERE purpose=:p AND retired_at IS NULL"),
                  {"n": datetime.now(UTC), "p": purpose})
        c.execute(text("INSERT INTO core.tenant_secrets (tenant_id, secret_id, purpose, ciphertext, created_by) "
                       "VALUES (:t, :s, :p, :c, :a)"),
                  {"t": tenant_id, "s": sid, "p": purpose, "c": crypto.encrypt(value, tenant_id), "a": actor})
    return f"tenant:{sid}"


def read_secret(engine: Engine, tenant_id: str, secret_id: str) -> str | None:
    with tenant_tx(tenant_id, engine) as c:
        blob = c.execute(text("SELECT ciphertext FROM core.tenant_secrets WHERE secret_id=:s AND retired_at IS NULL"),
                         {"s": secret_id}).scalar_one_or_none()
    return None if blob is None else crypto.decrypt(bytes(blob), tenant_id)
