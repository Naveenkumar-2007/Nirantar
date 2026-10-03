"""Tenant context. Every tenant-scoped operation reads the tenant from here (BB-§7)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from nirantar.core.errors import TenantContextMissing, TenantIsolationError

_TENANT_RE = re.compile(r"^ten_[0-9A-HJKMNP-TV-Z]{26}$|^ten_[a-z0-9_]{3,40}$")
_current: ContextVar[str | None] = ContextVar("nirantar_tenant", default=None)


def validate_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not _TENANT_RE.match(tenant_id):
        raise TenantIsolationError(f"invalid tenant id {tenant_id!r}")
    return tenant_id


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    validate_tenant_id(tenant_id)
    outer = _current.get()
    if outer is not None and outer != tenant_id:
        raise TenantIsolationError("nested tenant scopes for different tenants are not allowed")
    token = _current.set(tenant_id)
    try:
        yield tenant_id
    finally:
        try:
            _current.reset(token)
        except ValueError:
            # Framework dependencies (e.g. FastAPI generator deps) may run teardown in a different Context than
            # setup; restoring the outer value explicitly is equivalent and never leaks this tenant.
            _current.set(outer)


def current_tenant() -> str:
    tenant = _current.get()
    if tenant is None:
        raise TenantContextMissing("operation requires a tenant scope")
    return tenant


def assert_same_tenant(record_tenant_id: str) -> None:
    """Guard for any record read or written inside a tenant scope."""
    if record_tenant_id != current_tenant():
        raise TenantIsolationError("record belongs to a different tenant")


def scoped_key(*parts: str) -> str:
    """Cache/object/topic key always prefixed by tenant: 'ten_x:idem:abc'."""
    if any(":" in p for p in parts):
        raise ValueError("key parts must not contain ':'")
    return ":".join((current_tenant(), *parts))
