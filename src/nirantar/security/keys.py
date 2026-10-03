"""API keys and signing secrets.

Key format: nk_<tenant_id>.<key_id>.<secret>. The tenant is part of the key, so
authentication opens a tenant-scoped transaction directly and never scans other
tenants' keys. Only HMAC-SHA256(pepper, secret) is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, text

from nirantar.core.clock import Clock, SystemClock
from nirantar.core.errors import NirantarError
from nirantar.core.ids import new_id
from nirantar.core.tenancy import validate_tenant_id
from nirantar.db.session import tenant_tx
from nirantar.security.rbac import Principal, validate_roles


class AuthError(NirantarError):
    """Authentication failed. Message is deliberately generic."""


def _pepper() -> bytes:
    value = os.environ.get("API_KEY_PEPPER", "")
    if len(value) < 16:
        if os.environ.get("NIRANTAR_ENV", "local") in ("local", "test"):
            return b"local-dev-pepper-not-for-production"
        raise RuntimeError("API_KEY_PEPPER must be set (>=16 chars) outside local/test")
    return value.encode()


def _hash(secret: str) -> str:
    return hmac.new(_pepper(), secret.encode(), hashlib.sha256).hexdigest()


def hash_secret(secret: str) -> str:
    """Peppered HMAC used for every bearer secret Nirantar stores (API keys, OAuth codes and tokens)."""
    return _hash(secret)


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    plaintext: str  # shown once to the caller, never stored


def create_principal(engine: Engine, tenant_id: str, name: str, kind: str, roles: list[str]) -> str:
    validate_roles(roles)
    pid = new_id("prn")
    with tenant_tx(tenant_id, engine) as c:
        c.execute(
            text(
                "INSERT INTO core.principals (tenant_id, principal_id, kind, name, roles) "
                "VALUES (:t, :p, :k, :n, :r)"
            ),
            {"t": tenant_id, "p": pid, "k": kind, "n": name, "r": roles},
        )
    return pid


def issue_api_key(engine: Engine, tenant_id: str, principal_id: str, expires_at: datetime | None = None) -> IssuedKey:
    validate_tenant_id(tenant_id)
    key_id = new_id("key")
    secret = secrets.token_urlsafe(32)
    with tenant_tx(tenant_id, engine) as c:
        c.execute(
            text(
                "INSERT INTO core.api_keys (tenant_id, key_id, principal_id, key_hash, expires_at) "
                "VALUES (:t, :k, :p, :h, :e)"
            ),
            {"t": tenant_id, "k": key_id, "p": principal_id, "h": _hash(secret), "e": expires_at},
        )
    return IssuedKey(key_id, f"nk_{tenant_id}.{key_id}.{secret}")


def authenticate_api_key(engine: Engine, presented: str, clock: Clock | None = None) -> Principal:
    now = (clock or SystemClock()).now()
    try:
        body = presented.removeprefix("nk_")
        tenant_id, key_id, secret = body.split(".", 2)
        validate_tenant_id(tenant_id)
    except Exception as exc:
        raise AuthError("invalid credentials") from exc
    with tenant_tx(tenant_id, engine) as c:
        row = c.execute(
            text(
                "SELECT k.key_hash, k.expires_at, k.revoked_at, p.principal_id, p.kind, p.roles, p.status "
                "FROM core.api_keys k JOIN core.principals p "
                "ON p.tenant_id = k.tenant_id AND p.principal_id = k.principal_id "
                "WHERE k.tenant_id = :t AND k.key_id = :k"
            ),
            {"t": tenant_id, "k": key_id},
        ).one_or_none()
        if row is None or not hmac.compare_digest(row.key_hash, _hash(secret)):
            raise AuthError("invalid credentials")
        if row.revoked_at is not None or row.status != "active":
            raise AuthError("invalid credentials")
        if row.expires_at is not None and row.expires_at <= now:
            raise AuthError("invalid credentials")
        c.execute(
            text("UPDATE core.api_keys SET last_used_at = :n WHERE tenant_id = :t AND key_id = :k"),
            {"n": now, "t": tenant_id, "k": key_id},
        )
    return Principal(tenant_id, row.principal_id, row.kind, tuple(row.roles))


def revoke_api_key(engine: Engine, tenant_id: str, key_id: str, clock: Clock | None = None) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(
            text("UPDATE core.api_keys SET revoked_at = :n WHERE tenant_id = :t AND key_id = :k"),
            {"n": (clock or SystemClock()).now(), "t": tenant_id, "k": key_id},
        )
