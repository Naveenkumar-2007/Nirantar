"""Inbound WhatsApp: customer messages and delivery statuses (P6, ADR-0017).

Routing: the sender's number → peppered hash → the tenant/customer we last messaged on that number (comms.wa_routes).
A number we never messaged is not routed (and nothing is stored): Nirantar only talks to a merchant's own customers.

Per message (deduplicated on the provider message id — Meta retries webhooks):
  * text / button / interactive  → the reply text
  * voice note                    → Sarvam speech-to-text in the customer's language (codemix); the audio is kept as
                                    evidence; OTPs/PINs a customer reads out are redacted before anything is stored
  * image                         → payment-screenshot evidence (OCR + tamper signals + provider cross-check; never
                                    authoritative on its own)
  * document                      → stored as evidence
  * "STOP"                         → opted out of WhatsApp immediately (consent record), whatever case is open
Then `reply.received` goes to the outbox; the event bridge signals the customer's open DebitCycle, whose
record_reply step classifies the intent and acknowledges it.

Statuses (sent → delivered → read, or failed) update comms.messages; a status never moves backwards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text

from nirantar.channels.sink import phone_hash
from nirantar.channels.whatsapp import InboundMessage, StatusUpdate, WhatsAppCloud, WhatsAppError, parse_webhook
from nirantar.contracts.events import make_event
from nirantar.core import crypto
from nirantar.db.session import tenant_tx
from nirantar.db.stores import Outbox

STOP = re.compile(r"^\s*(stop|unsubscribe|बंद\s*करो|बंद|ఆపండి|ఆపు)\s*[.!]?\s*$", re.IGNORECASE)
RANK = {"sent": 1, "delivered": 2, "read": 3}


@dataclass
class InboundResult:
    messages: int = 0
    duplicates: int = 0
    unmatched: int = 0
    statuses: int = 0
    opted_out: int = 0
    events: list[str] = field(default_factory=list)


def _route(engine: Engine, number: str) -> tuple[str, str] | None:
    with engine.connect() as c:
        row = c.execute(text("SELECT tenant_id, customer_id FROM comms.wa_routes WHERE phone_hash=:h ORDER BY "
                             "last_outbound_at DESC NULLS LAST LIMIT 1"), {"h": phone_hash(number)}).one_or_none()
    return (row.tenant_id, row.customer_id) if row else None


def apply_status(engine: Engine, s: StatusUpdate) -> bool:
    with engine.connect() as c:
        tenant = c.execute(text("SELECT tenant_id FROM comms.wa_message_ids WHERE message_id=:m"),
                           {"m": s.message_id}).scalar_one_or_none()
    if tenant is None:
        return False
    with tenant_tx(tenant, engine) as c:
        cur = c.execute(text("SELECT status FROM comms.messages WHERE message_id=:m FOR UPDATE"),
                        {"m": s.message_id}).scalar_one_or_none()
        forward = s.status == "failed" or RANK.get(s.status, 0) > RANK.get(str(cur), 0)
        if cur is None or cur == "failed" or not forward:
            return False
        c.execute(text("UPDATE comms.messages SET status=:s, error_code=:e, error_title=:et, updated_at=:n WHERE "
                       "message_id=:m"), {"s": s.status, "e": s.error_code, "et": s.error_title, "n": s.at,
                                          "m": s.message_id})
    return True


def _evidence(engine: Engine, tenant: str, customer: str, case_id: str | None, m: InboundMessage,
              wa: WhatsAppCloud | None, speech: Any, provider: Any, language: str) -> tuple[str, dict[str, Any]]:
    """(reply text, attachment facts) for a media message."""
    from nirantar.evidence import objectstore
    from nirantar.evidence.service import EvidenceObject, save, screenshot_evidence
    from nirantar.voice.turn import intent_of, redact_secrets

    if wa is None or m.media_id is None:
        return m.text or "", {}
    data, mime = wa.download_media(m.media_id)
    now = datetime.now(UTC)
    if m.kind == "audio":
        uri, sha = objectstore.put(tenant, "voice_note", data, mime or "audio/ogg")
        transcript, lang, err = "", language, None
        if speech is not None:
            try:
                tr = speech.transcribe_file(data, mime or "audio/ogg", language)
                transcript, lang = tr.text, (tr.language_code or language)
            except Exception as exc:  # speech outage: keep the audio, a human can listen
                err = f"{type(exc).__name__}: {exc}"[:200]
        clean, secret = redact_secrets(transcript)
        ev = EvidenceObject(case_id, customer, "voice_note", uri,
                            {"transcript_redacted": clean, "intent": intent_of(clean), "secret_detected": secret,
                             "stt_error": err, "channel": "whatsapp"}, lang, 0.8 if clean else 0.0,
                            "stt" if clean else None, sha, now)
        with tenant_tx(tenant, engine) as c:
            save(c, tenant, ev)
        return clean, {"evidence_id": ev.evidence_id, "kind": "voice_note", "language": lang}
    if m.kind == "image":
        with tenant_tx(tenant, engine) as c:
            ev = screenshot_evidence(c, tenant, data, customer_id=customer, case_id=case_id, provider=provider,
                                     claimed_at=m.at, now=now)
        return m.text or "", {"evidence_id": ev.evidence_id, "kind": "screenshot",
                              "verdict": ev.extracted_fields.get("verdict")}
    uri, sha = objectstore.put(tenant, "document", data, mime or "application/octet-stream")
    ev = EvidenceObject(case_id, customer, "document", uri, {"mime": mime, "caption": m.text}, None, 0.0, None,
                        sha, now)
    with tenant_tx(tenant, engine) as c:
        save(c, tenant, ev)
    return m.text or "", {"evidence_id": ev.evidence_id, "kind": "document"}


def process_whatsapp(engine: Engine, payload: dict[str, Any], *, wa: WhatsAppCloud | None, speech: Any = None,
                     provider_for_tenant: Any = None) -> InboundResult:
    res = InboundResult()
    messages, statuses = parse_webhook(payload)
    for s in statuses:
        res.statuses += int(apply_status(engine, s))
    for m in messages:
        route = _route(engine, m.from_number)
        if route is None:
            res.unmatched += 1
            continue
        tenant, customer = route
        with tenant_tx(tenant, engine) as c:
            fresh = c.execute(text("INSERT INTO comms.messages (tenant_id, message_id, customer_id, channel, "
                                   "direction, kind, status, created_at, updated_at) VALUES (:t, :m, :c, 'whatsapp', "
                                   "'inbound', :k, 'received', :n, :n) ON CONFLICT DO NOTHING RETURNING 1"),
                              {"t": tenant, "m": m.message_id, "c": customer, "k": m.kind, "n": m.at}).one_or_none()
            cust = c.execute(text("SELECT preferred_language, consents FROM billing.customers WHERE customer_id=:c"),
                             {"c": customer}).one()
            case = c.execute(text("SELECT case_id, subject_id FROM ops.cases WHERE kind='debit_cycle' AND "
                                  "customer_id=:c AND status IN ('open','waiting','escalated') ORDER BY opened_at DESC "
                                  "LIMIT 1"), {"c": customer}).one_or_none()
        if fresh is None:
            res.duplicates += 1
            continue
        with engine.begin() as c:
            c.execute(text("UPDATE comms.wa_routes SET last_inbound_at=:n WHERE phone_hash=:h AND tenant_id=:t"),
                      {"n": m.at, "h": phone_hash(m.from_number), "t": tenant})
        language = cust.preferred_language or "en"
        reply, attachment = m.text or "", dict[str, Any]()
        if m.kind in ("audio", "image", "document", "video"):
            try:
                provider = provider_for_tenant(tenant) if provider_for_tenant else None
                reply, attachment = _evidence(engine, tenant, customer, case.case_id if case else None, m, wa,
                                              speech, provider, language)
            except WhatsAppError as exc:
                attachment = {"error": str(exc)[:200]}
        opted_out = bool(STOP.match(reply))
        with tenant_tx(tenant, engine) as c:
            # the inbox shows what the customer said (voice notes: the redacted transcript), encrypted at rest
            c.execute(text("UPDATE comms.messages SET body_enc=:b, evidence_id=:e WHERE message_id=:m"),
                      {"b": crypto.encrypt(reply, tenant) if reply else None,
                       "e": attachment.get("evidence_id"), "m": m.message_id})
            if opted_out:
                consents = cust.consents if isinstance(cust.consents, dict) else json.loads(cust.consents or "{}")
                consents["opted_out"] = sorted({*consents.get("opted_out", []), "whatsapp"})
                c.execute(text("UPDATE billing.customers SET consents=CAST(:j AS jsonb) WHERE customer_id=:c"),
                          {"j": json.dumps(consents), "c": customer})
                res.opted_out += 1
            ev = make_event(event_type="reply.received", version=1, tenant_id=tenant,
                            subject_id=case.subject_id if case else customer,
                            payload={"debit_id": case.subject_id if case else None, "customer_id": customer,
                                     "text": reply, "channel": "whatsapp", "message_id": m.message_id,
                                     "via": m.kind, "language": attachment.get("language", language),
                                     "attachment": attachment or None, "opted_out": opted_out},
                            source="channels/whatsapp", occurred_at=m.at)
            Outbox(c).add(ev)
        res.messages += 1
        res.events.append(ev.event_id)
    return res
