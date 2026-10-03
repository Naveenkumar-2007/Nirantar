"""Webhook ingress core (BB-§11): authenticate → persist raw → idempotency → publish.

Deliberately does no business processing, so it can answer providers well inside
Razorpay's 5-second timeout. Processing happens in `payments.processing`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from nirantar.contracts.events import make_event
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.db.stores import Outbox
from nirantar.payments.domain import PaymentProvider, ProviderError
from nirantar.security.secrets import SecretNotFound, resolve_secret

MAX_BODY_BYTES = 1_000_000
_REDACT = {"authorization", "cookie", "x-client-secret"}


@dataclass(frozen=True)
class IngestResult:
    status_code: int
    raw_event_id: str | None = None
    duplicate: bool = False
    reason: str = ""


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): ("<redacted>" if k.lower() in _REDACT else v[:512]) for k, v in headers.items()}


def ingest_webhook(engine: Engine, provider: PaymentProvider, tenant_id: str, headers: dict[str, str],
                   raw_body: bytes, clock: Clock | None = None) -> IngestResult:
    now = (clock or SystemClock()).now()
    if len(raw_body) > MAX_BODY_BYTES:
        return IngestResult(413, reason="body_too_large")
    with tenant_tx(tenant_id, engine) as c:
        account = c.execute(
            text("SELECT webhook_secret_ref FROM core.provider_accounts WHERE tenant_id=:t AND provider=:p "
                 "ORDER BY mode DESC LIMIT 1"),
            {"t": tenant_id, "p": provider.name},
        ).one_or_none()
    if account is None:
        return IngestResult(404, reason="unknown_account")  # don't reveal which part was wrong
    try:
        secret = resolve_secret(account.webhook_secret_ref, tenant_id=tenant_id, engine=engine)
    except SecretNotFound:
        return IngestResult(503, reason="webhook_secret_unavailable")  # provider will retry later
    if not provider.verify_webhook(headers, raw_body, secret):
        return IngestResult(401, reason="bad_signature")  # unauthenticated bodies are not stored
    try:
        normalized = provider.parse_webhook(headers, raw_body)
        event_type, provider_event_id = normalized.provider_event_type, normalized.provider_event_id
        mapped = normalized.event_type
    except (ProviderError, json.JSONDecodeError, KeyError) as exc:
        # Authentic but unmapped/unparseable: keep the raw bytes, ack so the provider doesn't disable us.
        event_type, provider_event_id, mapped = "unparsed", hashlib.sha256(raw_body).hexdigest(), None
        parse_error: str | None = str(exc)[:500]
    else:
        parse_error = None

    raw_event_id = new_id("raw")
    with tenant_tx(tenant_id, engine) as c:
        inserted = c.execute(
            text(
                "INSERT INTO ingest.provider_events (tenant_id, raw_event_id, provider, provider_event_id, event_type,"
                " signature_valid, received_at, headers, body, body_sha256, status, last_error) VALUES (:t, :r, :p, "
                ":pe, :et, true, :now, CAST(:h AS jsonb), :b, :bh, :st, :err) "
                "ON CONFLICT (tenant_id, provider, provider_event_id) DO NOTHING RETURNING raw_event_id"
            ),
            {"t": tenant_id, "r": raw_event_id, "p": provider.name, "pe": provider_event_id, "et": event_type,
             "now": now, "h": json.dumps(_safe_headers(headers)), "b": raw_body,
             "bh": hashlib.sha256(raw_body).hexdigest(), "st": "received" if mapped else "rejected",
             "err": parse_error},
        ).one_or_none()
        if inserted is None:
            return IngestResult(200, duplicate=True, reason="duplicate")
        if mapped:
            payload: dict[str, Any] = {"raw_event_id": raw_event_id, "provider": provider.name,
                                       "provider_event_type": event_type, "event_type": mapped}
            Outbox(c).add(make_event(event_type="provider.webhook_received", version=1, tenant_id=tenant_id,
                                     subject_id=raw_event_id, payload=payload, source=f"ingress/{provider.name}",
                                     occurred_at=now, clock=clock))
    return IngestResult(200, raw_event_id=raw_event_id)
