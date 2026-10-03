"""OIDC access-token verification and tenant memberships (P8, ADR-0018).

The dashboard signs people in with the identity provider (Keycloak, authorization code + PKCE) and calls the API
with the user's access token. The API verifies it locally: RS256 signature against the issuer's JWKS (discovered
from /.well-known/openid-configuration, keys cached and refreshed on rotation), `iss`, `aud` = nirantar-api, `exp`.
Roles come from Nirantar's own membership table, not from the token: a user's power inside a business is decided
by that business, and a compromised IdP admin cannot grant tenant roles.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from sqlalchemy import Engine, text

from nirantar.core.errors import NirantarError
from nirantar.db.session import tenant_tx
from nirantar.security.rbac import Principal, validate_roles

MAX_BUSINESSES_PER_USER = 10


class TokenInvalid(NirantarError):
    pass


@dataclass
class OIDCVerifier:
    issuer: str
    audience: str
    leeway_s: int = 30
    _jwks: jwt.PyJWKClient | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> OIDCVerifier | None:
        iss = os.environ.get("NIRANTAR_OIDC_ISSUER")
        return cls(iss.rstrip("/"), os.environ.get("NIRANTAR_OIDC_AUDIENCE", "nirantar-api")) if iss else None

    def _client(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._jwks is None:
                meta = httpx.get(f"{self.issuer}/.well-known/openid-configuration", timeout=10).json()
                if meta.get("issuer", "").rstrip("/") != self.issuer:
                    raise TokenInvalid("issuer metadata mismatch")
                self._jwks = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True, lifespan=3600)
            return self._jwks

    def verify(self, token: str) -> dict[str, Any]:
        try:
            key = self._client().get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token, key.key, algorithms=["RS256", "ES256"], audience=self.audience, issuer=self.issuer,
                leeway=self.leeway_s, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
        except (jwt.PyJWTError, httpx.HTTPError, KeyError) as exc:
            raise TokenInvalid("invalid or expired token") from exc
        if claims.get("typ") not in (None, "Bearer"):
            raise TokenInvalid("not an access token")
        return claims


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str | None
    name: str | None
    memberships: tuple[dict[str, Any], ...]


def touch_user(engine: Engine, claims: dict[str, Any]) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO core.users (user_sub, email, name, last_seen_at) VALUES (:s, :e, :n, :t) "
                       "ON CONFLICT (user_sub) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name, "
                       "last_seen_at=EXCLUDED.last_seen_at"),
                  {"s": claims["sub"], "e": claims.get("email"), "n": claims.get("name"), "t": datetime.now(UTC)})


def identity(engine: Engine, claims: dict[str, Any]) -> Identity:
    touch_user(engine, claims)
    with engine.connect() as c:
        rows = c.execute(text("SELECT tenant_id, roles FROM core.user_memberships WHERE user_sub=:s AND "
                              "status='active' ORDER BY created_at"), {"s": claims["sub"]}).all()
    out = []
    for r in rows:                     # core.tenants is RLS-protected: read each business inside its own scope
        with tenant_tx(r.tenant_id, engine) as c:
            t = c.execute(text("SELECT name, status FROM core.tenants WHERE tenant_id=:t"),
                          {"t": r.tenant_id}).one_or_none()
        if t is not None and t.status == "active":
            out.append({"tenant_id": r.tenant_id, "name": t.name, "roles": list(r.roles)})
    return Identity(claims["sub"], claims.get("email"), claims.get("name"), tuple(out))


def principal_for(ident: Identity, requested_tenant: str | None) -> Principal:
    """The user's principal inside one business: the requested one, or their only one."""
    if requested_tenant:
        m = next((m for m in ident.memberships if m["tenant_id"] == requested_tenant), None)
        if m is None:
            raise PermissionError("not a member of this business")
    elif len(ident.memberships) == 1:
        m = ident.memberships[0]
    elif not ident.memberships:
        raise LookupError("no business yet")
    else:
        raise LookupError("choose a business (X-Nirantar-Tenant)")
    return Principal(m["tenant_id"], f"usr_{ident.sub}", "user", tuple(m["roles"]))


def add_membership(engine: Engine, user_sub: str, tenant_id: str, roles: list[str], invited_by: str | None) -> None:
    validate_roles(roles)
    with engine.begin() as c:
        c.execute(text("INSERT INTO core.user_memberships (user_sub, tenant_id, roles, invited_by) VALUES "
                       "(:s, :t, :r, :i) ON CONFLICT (user_sub, tenant_id) DO UPDATE SET roles=EXCLUDED.roles, "
                       "status='active'"), {"s": user_sub, "t": tenant_id, "r": roles, "i": invited_by})


def count_owned(engine: Engine, user_sub: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM core.user_memberships WHERE user_sub=:s AND 'owner' = "
                                  "ANY(roles)"), {"s": user_sub}).scalar_one())


def now_ts() -> float:
    return time.time()
