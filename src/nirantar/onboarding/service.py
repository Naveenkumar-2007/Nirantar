"""Self-serve onboarding (P8.2, ADR-0019): connect payments → messaging → import history → readiness.

Every step reports its real state; nothing is marked done until it has been verified:
  * payments    the merchant's own Razorpay keys, checked live against Razorpay before they are stored (encrypted
                with the business's key); test vs live mode follows the key; live keys only in production
  * messaging   the WhatsApp channel available to this business
  * history     the OnboardingWorkflow (import → features → retention fit), with its stored outcome
  * readiness   which models have enough verified history (data health), never a promise
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from nirantar.billing.service import connect_provider
from nirantar.core.errors import NirantarError
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.security.keys import hash_secret
from nirantar.security.oidc import add_membership
from nirantar.security.vault import put_secret

KEY_ID = re.compile(r"^rzp_(test|live)_[A-Za-z0-9]{8,32}$")
INVITE_TTL = timedelta(days=7)
INVITE_ROLES = ("owner", "finance_approver", "compliance_officer", "ops_analyst", "viewer")


class OnboardingError(NirantarError):
    pass


def _settings(c: Any, tenant: str) -> dict[str, Any]:
    raw = c.execute(text("SELECT settings FROM core.tenants WHERE tenant_id=:t"), {"t": tenant}).scalar_one()
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def _merge_onboarding(engine: Engine, tenant: str, patch: dict[str, Any]) -> None:
    with tenant_tx(tenant, engine) as c:
        s = _settings(c, tenant)
        s["onboarding"] = {**s.get("onboarding", {}), **patch}
        c.execute(text("UPDATE core.tenants SET settings=CAST(:s AS jsonb) WHERE tenant_id=:t"),
                  {"s": json.dumps(s, default=str), "t": tenant})


# ---------------------------------------------------------------- payments
def connect_razorpay(engine: Engine, tenant: str, key_id: str, key_secret: str, actor: str,
                     verify: Any = None) -> dict[str, Any]:
    """Verify the keys with Razorpay, then store them encrypted. Returns the webhook URL and a webhook secret the
    merchant pastes into Razorpay (shown once)."""
    key_id, key_secret = key_id.strip(), key_secret.strip()
    m = KEY_ID.match(key_id)
    if not m or len(key_secret) < 12:
        raise OnboardingError("that doesn't look like a Razorpay key id (rzp_test_… / rzp_live_…) and secret")
    mode = m.group(1)
    if mode == "live" and os.environ.get("NIRANTAR_ENV", "local") != "production":
        raise OnboardingError("live keys can only be connected in production; use your test keys here")
    check = verify or _verify_razorpay
    check(key_id, key_secret)                                  # raises OnboardingError when Razorpay says no
    kid = put_secret(engine, tenant, "razorpay.key_id", key_id, actor)
    sec = put_secret(engine, tenant, "razorpay.key_secret", key_secret, actor)
    webhook_secret = secrets.token_urlsafe(24)
    wh = put_secret(engine, tenant, "razorpay.webhook_secret", webhook_secret, actor)
    with tenant_tx(tenant, engine) as c:
        connect_provider(c, tenant, "razorpay", mode, f"{kid};{sec}", wh)
    _merge_onboarding(engine, tenant, {"payments": {"provider": "razorpay", "mode": mode,
                                                    "verified_at": datetime.now(UTC).isoformat()}})
    base = os.environ.get("NIRANTAR_PUBLIC_URL", "http://localhost:18080")
    return {"provider": "razorpay", "mode": mode, "webhook_url": f"{base}/webhooks/razorpay/{tenant}",
            "webhook_secret": webhook_secret,
            "webhook_events": ["payment.captured", "payment.failed", "subscription.charged", "subscription.halted",
                               "subscription.cancelled", "payment.dispute.created", "payment.dispute.won",
                               "payment.dispute.lost", "token.confirmed", "token.cancelled", "token.paused",
                               "token.rejected", "payment_link.paid", "refund.processed"]}


def _verify_razorpay(key_id: str, key_secret: str) -> None:
    import httpx

    try:
        r = httpx.get("https://api.razorpay.com/v1/payments", params={"count": 1}, auth=(key_id, key_secret),
                      timeout=15)
    except httpx.HTTPError as exc:
        raise OnboardingError("couldn't reach Razorpay right now; please try again") from exc
    if r.status_code == 401:
        raise OnboardingError("Razorpay rejected these keys (check the key id and secret)")
    if r.status_code != 200:
        raise OnboardingError(f"Razorpay answered {r.status_code}; please try again")


# ---------------------------------------------------------------- checklist
def checklist(engine: Engine, tenant: str) -> dict[str, Any]:
    from nirantar.data import health

    with tenant_tx(tenant, engine) as c:
        s = _settings(c, tenant)
        name: str = c.execute(text("SELECT name FROM core.tenants WHERE tenant_id=:t"), {"t": tenant}).scalar_one()
        accounts = c.execute(text("SELECT provider, mode FROM core.provider_accounts")).all()
        plans = int(c.execute(text("SELECT count(*) FROM billing.plans WHERE active")).scalar_one())
        customers = int(c.execute(text("SELECT count(*) FROM billing.customers")).scalar_one())
        enrolled = int(c.execute(text("SELECT count(*) FROM billing.subscriptions WHERE status='active'")).scalar_one())
        links = c.execute(text("SELECT count(*) AS n, count(*) FILTER (WHERE status='paid') AS paid FROM "
                               "billing.payment_requests")).one()
    ob = s.get("onboarding", {})
    pay = ob.get("payments")
    payments = {"key": "payments", "title": "Connect payments",
                "status": "done" if accounts else "todo",
                "detail": (f"{accounts[0].provider} ({accounts[0].mode} mode)" if accounts else
                           "Connect your Razorpay account so Nirantar can see and act on your subscriptions"),
                "verified_at": pay.get("verified_at") if pay else None}
    wa_ready = bool(os.environ.get("WHATSAPP_PHONE_NUMBER_ID") and os.environ.get("WHATSAPP_ACCESS_TOKEN"))
    messaging = {"key": "messaging", "title": "Customer messaging",
                 "status": "done" if wa_ready else "blocked",
                 "detail": ("WhatsApp is available through Nirantar's number; your own number comes with "
                            "Embedded Signup" if wa_ready else "WhatsApp isn't configured for this deployment yet")}
    imp = {"running": "running", "done": "done", "failed": "failed", "started": "todo"}.get(ob.get("status", ""),
                                                                                       "todo")
    history = {"key": "history", "title": "Import your history", "status": imp if accounts else "blocked",
               "detail": (ob.get("reason") or "Nirantar imports your payment history and learns from it"
                          if imp != "done" else _summary(ob.get("summary") or {})),
               "started_at": ob.get("started_at"), "finished_at": ob.get("finished_at")}
    rep = health.latest(engine, tenant)
    ready = [{"model": k, "title": v["title"], "ready": v["ready"], "gaps": v["gaps"]}
             for k, v in ((rep or {}).get("readiness") or {}).items()]
    readiness = {"key": "readiness", "title": "AI readiness",
                 "status": "done" if any(r["ready"] for r in ready) else ("todo" if rep else "blocked"),
                 "detail": (f"{sum(r['ready'] for r in ready)} of {len(ready)} models have enough history"
                            if rep else "Appears after your history is imported"), "models": ready}
    plan = {"key": "plan", "title": "Create a plan", "href": "/plans",
            "status": "done" if plans else ("todo" if accounts else "blocked"),
            "detail": f"{plans} active plan{'s' if plans != 1 else ''}" if plans else
            "What you sell on repeat: name, price and how often (e.g. Chai Monthly, ₹499 every month)"}
    people = {"key": "customers", "title": "Add your customers", "href": "/customers",
              "status": "done" if customers and enrolled else ("todo" if plans else "blocked"),
              "detail": (f"{customers} customers · {enrolled} on a plan" if customers else
                         "Add them one by one or import your list (CSV) — with how each agreed to WhatsApp")}
    first = {"key": "first_payment", "title": "Collect your first payment", "href": "/customers",
             "status": "done" if links.paid else ("running" if links.n else ("todo" if enrolled else "blocked")),
             "detail": (f"{links.paid} paid through Nirantar" if links.paid else
                        f"{links.n} payment link{'s' if links.n != 1 else ''} sent — waiting for the first payment"
                        if links.n else "On the due date Nirantar sends the link; or open a customer and press "
                        "'Send payment link now'")}
    steps: list[dict[str, Any]] = [{"key": "business", "title": "Create your business", "status": "done",
                                    "detail": name},
                                   payments, plan, people, first, messaging,
                                   {**history, "optional": True},          # a new business has no history:
                                   {**readiness, "optional": True}]        # neither step blocks setup
    core = [st for st in steps if not st.get("optional")]
    return {"business": name, "steps": steps, "complete": all(st["status"] == "done" for st in core)}


def _summary(s: dict[str, Any]) -> str:
    parts = []
    if s.get("cycles") is not None:
        parts.append(f"{s['cycles']} billing cycles")
    if s.get("history_days") is not None:
        parts.append(f"{s['history_days']} days of history")
    return "Imported " + ", ".join(parts) if parts else "Imported"


def mark_import_started(engine: Engine, tenant: str) -> None:
    _merge_onboarding(engine, tenant, {"status": "running", "started_at": datetime.now(UTC).isoformat(),
                                       "reason": None})


# ---------------------------------------------------------------- team
def invite(engine: Engine, tenant: str, email: str, role: str, actor: str) -> dict[str, Any]:
    email = email.strip().lower()
    if role not in INVITE_ROLES:
        raise OnboardingError(f"role must be one of {', '.join(INVITE_ROLES)}")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise OnboardingError("enter a valid email address")
    iid = new_id("inv")
    with engine.begin() as c:
        c.execute(text("INSERT INTO core.invites (invite_id, tenant_id, email_hash, roles, invited_by, expires_at) "
                       "VALUES (:i, :t, :h, :r, :a, :e)"),
                  {"i": iid, "t": tenant, "h": hash_secret(f"email:{email}"), "r": [role], "a": actor,
                   "e": datetime.now(UTC) + INVITE_TTL})
    return {"invite_id": iid, "role": role, "expires_at": (datetime.now(UTC) + INVITE_TTL).isoformat()}


def claim_invites(engine: Engine, user_sub: str, email: str | None, email_verified: bool) -> int:
    """Called at sign-in. Only a VERIFIED email can claim an invite (otherwise anyone could register the address)."""
    if not email or not email_verified:
        return 0
    now = datetime.now(UTC)
    with engine.begin() as c:
        rows = c.execute(text("UPDATE core.invites SET accepted_by=:s, accepted_at=:n WHERE email_hash=:h AND "
                              "accepted_at IS NULL AND revoked_at IS NULL AND expires_at > :n "
                              "RETURNING tenant_id, roles, invited_by"),
                         {"s": user_sub, "n": now, "h": hash_secret(f"email:{email.strip().lower()}")}).all()
    for r in rows:
        add_membership(engine, user_sub, r.tenant_id, list(r.roles), invited_by=r.invited_by)
    return len(rows)


def team(engine: Engine, tenant: str) -> dict[str, Any]:
    with engine.connect() as c:
        members = c.execute(text("SELECT u.user_sub, u.email, u.name, m.roles, m.status, m.created_at FROM "
                                 "core.user_memberships m JOIN core.users u ON u.user_sub=m.user_sub WHERE "
                                 "m.tenant_id=:t ORDER BY m.created_at"), {"t": tenant}).all()
        invites = c.execute(text("SELECT invite_id, roles, invited_by, created_at, expires_at FROM core.invites "
                                 "WHERE tenant_id=:t AND accepted_at IS NULL AND revoked_at IS NULL AND "
                                 "expires_at > now() ORDER BY created_at DESC"), {"t": tenant}).all()
    return {"members": [dict(r._mapping) for r in members], "invites": [dict(r._mapping) for r in invites]}


def revoke_invite(engine: Engine, tenant: str, invite_id: str) -> bool:
    with engine.begin() as c:
        return c.execute(text("UPDATE core.invites SET revoked_at=now() WHERE invite_id=:i AND tenant_id=:t AND "
                              "accepted_at IS NULL AND revoked_at IS NULL RETURNING 1"),
                         {"i": invite_id, "t": tenant}).one_or_none() is not None
