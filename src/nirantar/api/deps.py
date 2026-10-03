"""API dependencies: authentication, permission checks, tenant-scoped DB connections, shared services."""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from nirantar.db.session import get_engine, tenant_tx
from nirantar.security.keys import AuthError, authenticate_api_key
from nirantar.security.oidc import Identity, OIDCVerifier, TokenInvalid, identity, principal_for
from nirantar.security.rbac import Permission, Principal


@dataclass
class Services:
    engine: Engine
    owner_engine: Engine | None = None
    provider: Any = None
    comms: Any = None
    llm: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


def _verifier(svc: Services) -> OIDCVerifier | None:
    if "oidc" not in svc.extra:
        svc.extra["oidc"] = OIDCVerifier.from_env()
    v: OIDCVerifier | None = svc.extra["oidc"]
    return v


def current_identity(authorization: str = Header(default=""), svc: Services = Depends(services)) -> Identity:
    """A signed-in person (OIDC access token), before any business is chosen."""
    token = authorization.removeprefix("Bearer ").strip()
    v = _verifier(svc)
    if not token or token.startswith("nk_") or v is None:
        raise HTTPException(401, "sign in required")
    try:
        return identity(svc.engine, v.verify(token))
    except TokenInvalid as exc:
        raise HTTPException(401, "invalid or expired session") from exc


def principal(authorization: str = Header(default=""), x_nirantar_tenant: str = Header(default=""),
              svc: Services = Depends(services)) -> Principal:
    """API keys (`nk_…`, services and scripts) or a signed-in person's OIDC token acting in one business."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "missing credentials")
    if token.startswith("nk_"):
        try:
            return authenticate_api_key(svc.engine, token)
        except AuthError as exc:
            raise HTTPException(401, "invalid credentials") from exc
    ident = current_identity(authorization, svc)
    try:
        return principal_for(ident, x_nirantar_tenant or None)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(409, {"code": "no_business" if not ident.memberships else "choose_business",
                                  "message": str(exc)}) from exc


def require(permission: Permission) -> Callable[[Principal], Principal]:
    def check(p: Principal = Depends(principal)) -> Principal:
        if not p.can(permission):
            raise HTTPException(403, f"missing permission {permission.value}")
        return p

    return check


def tenant_conn(p: Principal = Depends(require(Permission.READ)),
                svc: Services = Depends(services)) -> Iterator[Connection]:
    with tenant_tx(p.tenant_id, svc.engine) as c:
        yield c


def platform_admin(x_platform_key: str = Header(default="")) -> None:
    expected = os.environ.get("PLATFORM_ADMIN_KEY", "")
    if not expected or not hmac.compare_digest(expected, x_platform_key):
        raise HTTPException(401, "platform admin key required")


def default_services() -> Services:
    return Services(engine=get_engine())
