"""Call records, call tokens and call outcomes (P11, ADR-0026).

token   tenant . call_id . keyed hash — the only thing an outbound call carries; the gateway resolves everything else
context business name, amount due, language, customer first name — read server-side for the call's debit
outcome what the caller said (OTP-redacted, then encrypted), the intent, and its effect:
          promise_to_pay → an ops.promises row with the extracted date + a WhatsApp payment link (the customer asked)
          hardship       → an escalated case for a person
          opt_out        → voice consent withdrawn (opted_out += voice)
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from nirantar.core import crypto
from nirantar.core.crypto import lookup_hash
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx


class CallTokenError(ValueError):
    pass


def token_for(tenant_id: str, call_id: str) -> str:
    return f"{tenant_id}.{call_id}.{lookup_hash(f'voice-call:{call_id}', tenant_id)[:32]}"


def parse_token(token: str) -> tuple[str, str]:
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].startswith("ten_") or not parts[1].startswith("call_"):
        raise CallTokenError("invalid call token")
    if not hmac.compare_digest(lookup_hash(f"voice-call:{parts[1]}", parts[0])[:32], parts[2]):
        raise CallTokenError("invalid call token")
    return parts[0], parts[1]


def create(engine: Engine, tenant_id: str, customer_id: str, debit_id: str | None, language: str,
           now: datetime) -> str:
    call_id = new_id("call")
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO comms.calls (tenant_id, call_id, debit_id, customer_id, language, status, "
                       "created_at) VALUES (:t, :c, :d, :cu, :l, 'requested', :n)"),
                  {"t": tenant_id, "c": call_id, "d": debit_id, "cu": customer_id, "l": language, "n": now})
    return call_id


def context(engine: Engine, token: str) -> dict[str, Any]:
    """Everything the voice agent may say, resolved server-side from the signed token."""
    tenant_id, call_id = parse_token(token)
    with tenant_tx(tenant_id, engine) as c:
        r = c.execute(text(
            "SELECT k.call_id, k.debit_id, k.customer_id, k.language, t.name AS business, cu.display_name, "
            "d.amount_minor FROM comms.calls k JOIN core.tenants t ON t.tenant_id=k.tenant_id JOIN billing.customers "
            "cu ON cu.tenant_id=k.tenant_id AND cu.customer_id=k.customer_id LEFT JOIN billing.debits d ON "
            "d.tenant_id=k.tenant_id AND d.debit_id=k.debit_id WHERE k.tenant_id=:t AND k.call_id=:c"),
            {"t": tenant_id, "c": call_id}).one_or_none()
    if r is None:
        raise CallTokenError("unknown call")
    amount = f"₹{int(r.amount_minor or 0) / 100:,.2f}" if r.amount_minor else ""
    return {"tenant_id": tenant_id, "call_id": call_id, "debit_id": r.debit_id, "customer_id": r.customer_id,
            "language": r.language, "merchant": r.business, "amount_text": amount,
            "first_name": (r.display_name or "").split(" ")[0]}


def set_status(engine: Engine, tenant_id: str, call_id: str, status: str, *, call_sid: str | None = None,
               duration_s: int | None = None, now: datetime) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text(
            "UPDATE comms.calls SET status=:s, call_sid=coalesce(:sid, call_sid), duration_s=coalesce(:d, duration_s), "
            "started_at=CASE WHEN :s='in_progress' AND started_at IS NULL THEN CAST(:n AS timestamptz) ELSE started_at "
            "END, ended_at=CASE WHEN :s IN ('completed','no_answer','busy','failed','canceled') THEN "
            "CAST(:n AS timestamptz) ELSE ended_at END WHERE tenant_id=:t AND call_id=:c"),
            {"s": status, "sid": call_sid, "d": duration_s, "n": now, "t": tenant_id, "c": call_id})


def finish(engine: Engine, ctx: dict[str, Any], turns: list[dict[str, Any]], intent: str | None,
           now: datetime) -> dict[str, Any]:
    """Store the (already redacted) transcript encrypted and apply the call's outcome."""
    from nirantar.agents.promise import extract_promise_date

    tenant_id, call_id = ctx["tenant_id"], ctx["call_id"]
    caller_said = " ".join(t["text"] for t in turns if t.get("role") == "caller")
    effect: dict[str, Any] = {"intent": intent or "no_commitment"}
    with tenant_tx(tenant_id, engine) as c:
        promise_id = None
        if intent == "promise_to_pay" and ctx.get("debit_id"):
            when = extract_promise_date(caller_said, now.date())
            c.execute(text("UPDATE ops.promises SET status='superseded', resolved_at=:n WHERE tenant_id=:t AND "
                           "debit_id=:d AND status='open'"), {"n": now, "t": tenant_id, "d": ctx["debit_id"]})
            promise_id = new_id("prm")
            c.execute(text("INSERT INTO ops.promises (tenant_id, promise_id, debit_id, customer_id, promised_date, "
                           "source, quote, status, created_at) VALUES (:t, :p, :d, :c, :pd, 'voice_note', :q, 'open', "
                           ":n)"),
                      {"t": tenant_id, "p": promise_id, "d": ctx["debit_id"], "c": ctx["customer_id"], "pd": when,
                       "q": caller_said[:300] or None, "n": now})
            effect["promised_date"] = when.isoformat() if when else None
        elif intent == "hardship":
            c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, summary, "
                           "opened_at) VALUES (:t, :k, 'collections', :s, :c, 'escalated', CAST(:sum AS jsonb), :n)"),
                      {"t": tenant_id, "k": new_id("cas"), "s": ctx.get("debit_id") or call_id,
                       "c": ctx["customer_id"], "sum": json.dumps({"reason": "hardship on a voice call",
                                                                   "call_id": call_id}), "n": now})
        elif intent == "opt_out":
            c.execute(text("UPDATE billing.customers SET consents = jsonb_set(consents, '{opted_out}', "
                           "to_jsonb(ARRAY(SELECT DISTINCT unnest(coalesce(ARRAY(SELECT jsonb_array_elements_text("
                           "consents->'opted_out')), '{}') || ARRAY['voice'])))) WHERE customer_id=:c"),
                      {"c": ctx["customer_id"]})
        c.execute(text("UPDATE comms.calls SET outcome=:o, transcript_enc=:tr, promise_id=:p WHERE tenant_id=:t AND "
                       "call_id=:c"),
                  {"o": effect["intent"], "tr": crypto.encrypt(json.dumps(turns, ensure_ascii=False), tenant_id),
                   "p": promise_id, "t": tenant_id, "c": call_id})
    effect["promise_id"] = promise_id
    return effect


def transcript(engine: Engine, tenant_id: str, call_id: str) -> list[dict[str, Any]]:
    with tenant_tx(tenant_id, engine) as c:
        enc: Any = c.execute(text("SELECT transcript_enc FROM comms.calls WHERE tenant_id=:t AND call_id=:c"),
                        {"t": tenant_id, "c": call_id}).scalar_one()
    out: list[dict[str, Any]] = json.loads(crypto.decrypt(bytes(enc), tenant_id)) if enc else []
    return out
