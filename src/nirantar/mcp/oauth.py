"""OAuth 2.1 authorization server for the Nirantar MCP server (P7, ADR-0016).

Flow (MCP authorization spec: OAuth 2.1 + PKCE, RFC 7591 dynamic registration, RFC 8707 resource indicators):
  1. the MCP client registers itself (/register) and starts /authorize with a PKCE challenge;
  2. we store the request and send the browser to the merchant's DASHBOARD consent page — the merchant is already
     signed in there, so no secret is ever typed into an OAuth page;
  3. a tenant admin approves (choosing scopes) → a grant + single-use code (5 min) bound to the tenant;
  4. /token exchanges the code (PKCE verified by the SDK) for an access token (1 h) and a refresh token (30 d).
Refresh tokens rotate; presenting an already-used refresh token revokes the whole grant (token theft signal).
Tokens look like `nat_<tenant>.<secret>`: the tenant id selects the RLS scope, only an HMAC hash is stored.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.core.tenancy import validate_tenant_id
from nirantar.db.session import tenant_tx
from nirantar.security.keys import hash_secret
from nirantar.security.rbac import Permission, Principal

SCOPES = ("nirantar:read", "nirantar:act", "nirantar:money")
DEFAULT_SCOPES = ["nirantar:read"]
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
PREFIX = {"code": "nac_", "access": "nat_", "refresh": "nrt_"}


class ConsentError(ValueError):
    pass


class NirantarCode(AuthorizationCode):
    tenant_id: str
    grant_id: str


class NirantarRefresh(RefreshToken):
    tenant_id: str
    grant_id: str


class NirantarAccess(AccessToken):
    tenant_id: str
    grant_id: str


def _mint(kind: str, tenant_id: str) -> str:
    return f"{PREFIX[kind]}{tenant_id}.{secrets.token_urlsafe(32)}"


def _tenant_of(kind: str, presented: str) -> str | None:
    if not presented.startswith(PREFIX[kind]) or "." not in presented:
        return None
    tenant = presented[len(PREFIX[kind]):].split(".", 1)[0]
    try:
        return validate_tenant_id(tenant)
    except Exception:
        return None


@dataclass
class OAuthStore:
    """Synchronous persistence; NirantarOAuthProvider wraps it for the async SDK."""

    engine: Engine
    consent_url: str                         # dashboard page, e.g. http://localhost:3010/connect/mcp

    # ---------------------------------------------------------------- clients and requests (platform tables)
    def register_client(self, info: OAuthClientInformationFull) -> None:
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO core.oauth_clients (client_id, info) VALUES (:c, CAST(:i AS jsonb))"),
                      {"c": info.client_id, "i": info.model_dump_json(exclude_none=True)})

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.engine.connect() as c:
            info = c.execute(text("SELECT info FROM core.oauth_clients WHERE client_id=:c"),
                             {"c": client_id}).scalar_one_or_none()
        if info is None:
            return None
        return OAuthClientInformationFull.model_validate(info if isinstance(info, dict) else json.loads(info))

    def create_request(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        scopes = params.scopes or DEFAULT_SCOPES
        if unknown := set(scopes) - set(SCOPES):
            raise AuthorizeError("invalid_scope", f"unknown scopes {sorted(unknown)}")
        rid = "oar_" + secrets.token_urlsafe(24)
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO core.oauth_requests (request_id, client_id, params, expires_at) VALUES "
                           "(:r, :c, CAST(:p AS jsonb), :e)"),
                      {"r": rid, "c": client.client_id, "e": datetime.now(UTC) + REQUEST_TTL,
                       "p": json.dumps({**params.model_dump(mode="json"), "scopes": scopes})})
        return f"{self.consent_url}?request={rid}"

    def describe_request(self, request_id: str) -> dict[str, Any]:
        """What the consent page shows. Raises ConsentError when unknown, expired or already decided."""
        with self.engine.connect() as c:
            row = c.execute(text("SELECT r.params, r.expires_at, r.decided_at, k.info FROM core.oauth_requests r "
                                 "JOIN core.oauth_clients k ON k.client_id=r.client_id WHERE r.request_id=:r"),
                            {"r": request_id}).one_or_none()
        if row is None:
            raise ConsentError("unknown request")
        if row.decided_at is not None:
            raise ConsentError("this request was already decided")
        if row.expires_at <= datetime.now(UTC):
            raise ConsentError("this request expired; start the connection again from your MCP client")
        params = row.params if isinstance(row.params, dict) else json.loads(row.params)
        info = row.info if isinstance(row.info, dict) else json.loads(row.info)
        return {"request_id": request_id, "client_name": info.get("client_name") or info["client_id"],
                "client_id": info["client_id"], "redirect_uri": params["redirect_uri"],
                "scopes": params["scopes"], "resource": params.get("resource"),
                "expires_at": row.expires_at.isoformat()}

    def decide(self, principal: Principal, request_id: str, *, approve: bool, scopes: list[str] | None) -> str:
        """The tenant admin's decision → the URL the browser goes back to (code, or error=access_denied)."""
        if not principal.can(Permission.TENANT_ADMIN):
            raise PermissionError("only a tenant admin can connect an MCP client")
        req = self.describe_request(request_id)
        now = datetime.now(UTC)
        with self.engine.begin() as c:
            row = c.execute(text("UPDATE core.oauth_requests SET decided_at=:n, decision=:d WHERE request_id=:r AND "
                                 "decided_at IS NULL RETURNING params"),
                            {"n": now, "d": "approved" if approve else "denied", "r": request_id}).one_or_none()
        if row is None:
            raise ConsentError("this request was already decided")
        params = row.params if isinstance(row.params, dict) else json.loads(row.params)
        if not approve:
            return construct_redirect_uri(params["redirect_uri"], error="access_denied", state=params.get("state"))
        granted = [s for s in (scopes or req["scopes"]) if s in req["scopes"]]
        if not granted:
            raise ConsentError("grant at least one of the requested scopes")
        grant_id, code = new_id("grt"), _mint("code", principal.tenant_id)
        with tenant_tx(principal.tenant_id, self.engine) as c:
            c.execute(text("INSERT INTO core.oauth_grants (tenant_id, grant_id, client_id, principal_id, scopes, "
                           "resource) VALUES (:t, :g, :c, :p, :s, :r)"),
                      {"t": principal.tenant_id, "g": grant_id, "c": req["client_id"], "p": principal.principal_id,
                       "s": granted, "r": params.get("resource")})
            self._store(c, principal.tenant_id, code, "code", grant_id, now + CODE_TTL, params)
        return construct_redirect_uri(params["redirect_uri"], code=code, state=params.get("state"))

    # ---------------------------------------------------------------- codes and tokens (tenant tables, RLS)
    @staticmethod
    def _store(c: Any, tenant: str, plaintext: str, kind: str, grant_id: str, expires: datetime,
               params: dict[str, Any] | None = None) -> None:
        c.execute(text("INSERT INTO core.oauth_secrets (tenant_id, secret_hash, kind, grant_id, params, expires_at) "
                       "VALUES (:t, :h, :k, :g, CAST(:p AS jsonb), :e)"),
                  {"t": tenant, "h": hash_secret(plaintext), "k": kind, "g": grant_id,
                   "p": json.dumps(params or {}), "e": expires})

    def _lookup(self, kind: str, presented: str) -> Any:
        tenant = _tenant_of(kind, presented)
        if tenant is None:
            return None
        with tenant_tx(tenant, self.engine) as c:
            return c.execute(text(
                "SELECT s.tenant_id, s.grant_id, s.params, s.expires_at, s.used_at, g.client_id, g.principal_id, "
                "g.scopes, g.resource, g.revoked_at FROM core.oauth_secrets s JOIN core.oauth_grants g ON "
                "g.tenant_id=s.tenant_id AND g.grant_id=s.grant_id WHERE s.secret_hash=:h AND s.kind=:k"),
                {"h": hash_secret(presented), "k": kind}).one_or_none()

    def load_code(self, code: str) -> NirantarCode | None:
        row = self._lookup("code", code)
        if row is None or row.used_at is not None or row.revoked_at is not None:
            return None
        p = row.params if isinstance(row.params, dict) else json.loads(row.params)
        return NirantarCode(code=code, scopes=list(row.scopes), expires_at=row.expires_at.timestamp(),
                            client_id=row.client_id, code_challenge=p["code_challenge"],
                            redirect_uri=AnyUrl(p["redirect_uri"]),
                            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                            resource=row.resource, subject=row.principal_id, tenant_id=row.tenant_id,
                            grant_id=row.grant_id)

    def _use_once(self, tenant: str, presented: str) -> bool:
        with tenant_tx(tenant, self.engine) as c:
            return c.execute(text("UPDATE core.oauth_secrets SET used_at=now() WHERE secret_hash=:h AND used_at IS "
                                  "NULL RETURNING 1"), {"h": hash_secret(presented)}).one_or_none() is not None

    def _issue(self, tenant: str, grant_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh, now = _mint("access", tenant), _mint("refresh", tenant), datetime.now(UTC)
        with tenant_tx(tenant, self.engine) as c:
            self._store(c, tenant, access, "access", grant_id, now + ACCESS_TTL, {"scopes": scopes})
            self._store(c, tenant, refresh, "refresh", grant_id, now + REFRESH_TTL, {"scopes": scopes})
        return OAuthToken(access_token=access, expires_in=int(ACCESS_TTL.total_seconds()), scope=" ".join(scopes),
                          refresh_token=refresh)

    def exchange_code(self, code: NirantarCode) -> OAuthToken:
        if not self._use_once(code.tenant_id, code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(code.tenant_id, code.grant_id, code.scopes)

    def load_refresh(self, presented: str) -> NirantarRefresh | None:
        row = self._lookup("refresh", presented)
        if row is None or row.revoked_at is not None:
            return None
        if row.used_at is not None:                      # rotated token presented again: assume theft
            self.revoke_grant(row.tenant_id, row.grant_id, "refresh token reuse detected")
            return None
        return NirantarRefresh(token=presented, client_id=row.client_id, scopes=list(row.scopes),
                               expires_at=int(row.expires_at.timestamp()), resource=row.resource,
                               subject=row.principal_id, tenant_id=row.tenant_id, grant_id=row.grant_id)

    def exchange_refresh(self, rt: NirantarRefresh, scopes: list[str]) -> OAuthToken:
        narrowed = [s for s in (scopes or rt.scopes) if s in rt.scopes]
        if not narrowed:
            raise TokenError("invalid_scope", "requested scopes exceed the grant")
        if not self._use_once(rt.tenant_id, rt.token):
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue(rt.tenant_id, rt.grant_id, narrowed)

    def load_access(self, presented: str) -> NirantarAccess | None:
        row = self._lookup("access", presented)
        if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
            return None
        p = row.params if isinstance(row.params, dict) else json.loads(row.params)
        return NirantarAccess(token=presented, client_id=row.client_id, scopes=list(p.get("scopes", row.scopes)),
                              expires_at=int(row.expires_at.timestamp()), resource=row.resource,
                              subject=row.principal_id, tenant_id=row.tenant_id, grant_id=row.grant_id)

    def revoke_grant(self, tenant: str, grant_id: str, reason: str) -> bool:
        with tenant_tx(tenant, self.engine) as c:
            return c.execute(text("UPDATE core.oauth_grants SET revoked_at=now(), revoked_reason=:r WHERE "
                                  "grant_id=:g AND revoked_at IS NULL RETURNING 1"),
                             {"r": reason[:200], "g": grant_id}).one_or_none() is not None

    def list_grants(self, tenant: str) -> list[dict[str, Any]]:
        with tenant_tx(tenant, self.engine) as c:
            rows = c.execute(text(
                "SELECT g.grant_id, g.client_id, k.info->>'client_name' AS client_name, g.principal_id, g.scopes, "
                "g.created_at, g.revoked_at, g.revoked_reason, (SELECT max(s.created_at) FROM core.oauth_secrets s "
                "WHERE s.tenant_id=g.tenant_id AND s.grant_id=g.grant_id AND s.kind='access') AS last_token_at "
                "FROM core.oauth_grants g JOIN core.oauth_clients k ON k.client_id=g.client_id "
                "ORDER BY g.created_at DESC")).all()
        return [dict(r._mapping) for r in rows]


class NirantarOAuthProvider:
    """`OAuthAuthorizationServerProvider` for the MCP SDK (async facade over OAuthStore)."""

    def __init__(self, store: OAuthStore) -> None:
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(self.store.get_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(self.store.register_client, client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        return await asyncio.to_thread(self.store.create_request, client, params)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> NirantarCode | None:
        code = await asyncio.to_thread(self.store.load_code, authorization_code)
        return code if code is not None and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: NirantarCode) -> OAuthToken:
        return await asyncio.to_thread(self.store.exchange_code, authorization_code)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> NirantarRefresh | None:
        rt = await asyncio.to_thread(self.store.load_refresh, refresh_token)
        return rt if rt is not None and rt.client_id == client.client_id else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: NirantarRefresh,
                                     scopes: list[str]) -> OAuthToken:
        return await asyncio.to_thread(self.store.exchange_refresh, refresh_token, scopes)

    async def load_access_token(self, token: str) -> NirantarAccess | None:
        at = await asyncio.to_thread(self.store.load_access, token)
        if at is not None and at.expires_at is not None and at.expires_at < time.time():
            return None
        return at

    async def exchange_identity_assertion(self, client: OAuthClientInformationFull,
                                          params: IdentityAssertionParams) -> OAuthToken:
        """Enterprise IdP assertions (SEP-990) are not accepted yet: OIDC arrives with P8."""
        raise TokenError("unsupported_grant_type", "identity assertion grants are not enabled")

    async def revoke_token(self, token: NirantarAccess | NirantarRefresh) -> None:
        await asyncio.to_thread(self.store.revoke_grant, token.tenant_id, token.grant_id, "revoked by client")
