"""Approval workflow.

request_approval() → pending row (the maker is recorded)
decide()          → an approver with approvals:decide who is NOT the maker grants/denies;
                    a grant returns a signed token scoped to (tenant, approval, action, tool, params_hash)
redeem()          → the MCP gateway verifies signature, expiry, scope and params hash, then atomically
                    marks the approval 'used' — a token works exactly once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.errors import NirantarError
from nirantar.core.ids import new_id
from nirantar.security.rbac import Permission, Principal


class ApprovalError(NirantarError):
    pass


def _key() -> bytes:
    value = os.environ.get("APPROVAL_SIGNING_KEY", "")
    if len(value) < 32:
        if os.environ.get("NIRANTAR_ENV", "local") in ("local", "test"):
            return b"local-dev-approval-signing-key-000000"
        raise RuntimeError("APPROVAL_SIGNING_KEY must be >= 32 chars outside local/test")
    return value.encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def _verify_sig(token: str) -> dict[str, Any]:
    try:
        body, mac = token.split(".", 1)
    except ValueError as exc:
        raise ApprovalError("malformed token") from exc
    expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, mac):
        raise ApprovalError("bad token signature")
    return dict(json.loads(_unb64(body)))


def request_approval(conn: Connection, tenant_id: str, action_id: str, tool_name: str, params_hash: str,
                     requested_by: str, reason: str, now: datetime, ttl: timedelta = timedelta(hours=24)) -> str:
    aid = new_id("apr")
    conn.execute(
        text("INSERT INTO ops.approvals (tenant_id, approval_id, action_id, reason, status, requested_at, expires_at, "
             "requested_by, tool_name, params_hash) VALUES (:t, :a, :ac, :r, 'pending', :n, :e, :rb, :tn, :ph)"),
        {"t": tenant_id, "a": aid, "ac": action_id, "r": reason, "n": now, "e": now + ttl, "rb": requested_by,
         "tn": tool_name, "ph": params_hash},
    )
    return aid


def decide(conn: Connection, approver: Principal, approval_id: str, grant: bool, now: datetime,
           token_ttl: timedelta = timedelta(minutes=30)) -> str | None:
    if not approver.can(Permission.APPROVALS_DECIDE):
        raise ApprovalError("approver lacks approvals:decide")
    row = conn.execute(
        text("SELECT * FROM ops.approvals WHERE tenant_id=:t AND approval_id=:a FOR UPDATE"),
        {"t": approver.tenant_id, "a": approval_id},
    ).one_or_none()
    if row is None:
        raise ApprovalError("approval not found")
    if row.status != "pending":
        raise ApprovalError(f"approval is {row.status}")
    if row.expires_at <= now:
        conn.execute(text("UPDATE ops.approvals SET status='expired' WHERE tenant_id=:t AND approval_id=:a"),
                     {"t": approver.tenant_id, "a": approval_id})
        raise ApprovalError("approval request expired")
    if f"user:{approver.principal_id}" == row.requested_by:
        raise ApprovalError("maker cannot approve their own action")
    jti = new_id("jti")
    conn.execute(
        text("UPDATE ops.approvals SET status=:s, decided_by=:d, decided_at=:n, token_jti=:j "
             "WHERE tenant_id=:t AND approval_id=:a"),
        {"s": "granted" if grant else "denied", "d": approver.principal_id, "n": now, "j": jti if grant else None,
         "t": approver.tenant_id, "a": approval_id},
    )
    if not grant:
        return None
    return sign({"v": 1, "tid": approver.tenant_id, "aid": approval_id, "act": row.action_id,
                 "tool": row.tool_name, "ph": row.params_hash, "jti": jti,
                 "exp": int((now + token_ttl).timestamp())})


def redeem(conn: Connection, token: str, tenant_id: str, tool_name: str, params_hash: str, now: datetime) -> str:
    """Verify and consume a token. Returns the approval_id. Raises ApprovalError on any mismatch."""
    claims = _verify_sig(token)
    if claims.get("tid") != tenant_id:
        raise ApprovalError("token is for another tenant")
    if claims.get("tool") != tool_name:
        raise ApprovalError("token is scoped to a different tool")
    if claims.get("ph") != params_hash:
        raise ApprovalError("parameters changed after approval")
    if int(claims.get("exp", 0)) < int(now.timestamp()):
        raise ApprovalError("token expired")
    used = conn.execute(
        text("UPDATE ops.approvals SET status='used', used_at=:n WHERE tenant_id=:t AND approval_id=:a "
             "AND status='granted' AND token_jti=:j RETURNING approval_id"),
        {"n": now, "t": tenant_id, "a": claims["aid"], "j": claims["jti"]},
    ).one_or_none()
    if used is None:
        raise ApprovalError("token already used or approval not granted")
    return str(claims["aid"])
