from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.security.keys import (
    AuthError,
    authenticate_api_key,
    create_principal,
    issue_api_key,
    revoke_api_key,
)
from nirantar.security.rbac import Permission

pytestmark = pytest.mark.integration


def _tenant(engine: Engine) -> str:
    t = new_id("ten")
    with tenant_tx(t, engine) as c:
        c.execute(text("INSERT INTO core.tenants (tenant_id, name) VALUES (:t, 'x')"), {"t": t})
    return t


def test_api_key_roundtrip_permissions_and_revocation(app_engine: Engine) -> None:
    t = _tenant(app_engine)
    pid = create_principal(app_engine, t, "fin", "user", ["finance_approver"])
    key = issue_api_key(app_engine, t, pid)
    principal = authenticate_api_key(app_engine, key.plaintext)
    assert principal.tenant_id == t
    assert principal.can(Permission.APPROVALS_DECIDE)
    assert not principal.can(Permission.POLICY_ADMIN)

    revoke_api_key(app_engine, t, key.key_id)
    with pytest.raises(AuthError):
        authenticate_api_key(app_engine, key.plaintext)


def test_tampered_or_cross_tenant_keys_fail(app_engine: Engine) -> None:
    a, b = _tenant(app_engine), _tenant(app_engine)
    key = issue_api_key(app_engine, a, create_principal(app_engine, a, "svc", "service", ["service_worker"]))
    _, rest = key.plaintext.split(".", 1)
    with pytest.raises(AuthError):  # same key id + secret presented under another tenant
        authenticate_api_key(app_engine, f"nk_{b}.{rest}")
    with pytest.raises(AuthError):
        authenticate_api_key(app_engine, key.plaintext[:-2] + "xx")
    with pytest.raises(AuthError):
        authenticate_api_key(app_engine, "garbage")


def test_expired_key_fails(app_engine: Engine) -> None:
    t = _tenant(app_engine)
    pid = create_principal(app_engine, t, "v", "user", ["viewer"])
    key = issue_api_key(app_engine, t, pid, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(AuthError):
        authenticate_api_key(app_engine, key.plaintext)


def test_unknown_role_rejected(app_engine: Engine) -> None:
    t = _tenant(app_engine)
    with pytest.raises(ValueError):
        create_principal(app_engine, t, "x", "user", ["god_mode"])
