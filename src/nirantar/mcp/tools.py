"""Tool definitions for the thin end-to-end slice (gateway, comms, customer, policy, experiment, ledger).

Money safety (BB-§8): no tool accepts an amount from an agent. Amounts and dates are loaded from the
debit record; messages that mention ₹ amounts are checked against the real debit before sending.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.comms.sink import OutboundTemplate
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.experiments import service as experiments
from nirantar.mcp.gateway import Tool, ToolContext
from nirantar.payments.domain import LinkRequest, PaymentProvider
from nirantar.policy.engine import ActionRequest, evaluate
from nirantar.settings import service as settings
from nirantar.settings import templates
from nirantar.verifier.payments import verify_capture

# 'Rs'/'INR' must not be preceded by a letter/digit: ids and URLs contain e.g. '...NRS331...'.
_AMOUNT_RE = re.compile(r"(?:₹|(?<![A-Za-z0-9])(?:rs\.?|inr))\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers
def _customer(conn: Connection, tenant_id: str, customer_id: str) -> Any:
    return conn.execute(
        text("SELECT customer_id, segment, preferred_language, timezone, consents, display_name "
             "FROM billing.customers WHERE tenant_id=:t AND customer_id=:c"),
        {"t": tenant_id, "c": customer_id}).one()


def _contacts_7d(conn: Connection, tenant_id: str, customer_id: str, now: Any) -> int:
    return int(conn.execute(
        text("SELECT count(*) FROM ops.contacts WHERE tenant_id=:t AND customer_id=:c AND at > :since "
             "AND purpose <> 'mandatory'"),
        {"t": tenant_id, "c": customer_id, "since": now - timedelta(days=7)}).scalar_one())


def _debit(conn: Connection, tenant_id: str, debit_id: str) -> Any:
    return conn.execute(
        text("SELECT d.debit_id, d.customer_id, d.amount_minor, d.currency, d.scheduled_for, d.status, "
             "d.provider_payment_id, s.provider FROM billing.debits d JOIN billing.subscriptions s "
             "ON s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id "
             "WHERE d.tenant_id=:t AND d.debit_id=:d"), {"t": tenant_id, "d": debit_id}).one()


def _consents(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def contact_request(conn: Connection, ctx: ToolContext, customer_id: str, action_kind: str, purpose: str,
                    message_text: str | None = None, mandatory_kind: str | None = None) -> ActionRequest:
    cust = _customer(conn, ctx.tenant_id, customer_id)
    consents = _consents(cust.consents)
    return ActionRequest(
        tenant_id=ctx.tenant_id, action_kind=action_kind, segment=cust.segment, now_utc=ctx.now,
        customer_timezone=cust.timezone, purpose=purpose,
        consents={k: bool(v) for k, v in consents.items() if k != "opted_out"},
        opted_out_channels=frozenset(consents.get("opted_out", [])),
        contacts_last_7d=_contacts_7d(conn, ctx.tenant_id, customer_id, ctx.now),
        message_text=message_text, mandatory_kind=mandatory_kind,
    )


_URL = re.compile(r"https?://\S+")


def first_name(display_name: str | None) -> str:
    return (display_name or "").split(" ")[0]


def rupees(m: Money) -> str:
    return f"₹{m.to_decimal():,.2f}"


def notice_channel(services: Any) -> str:
    """Mandatory notices go by SMS when the deployment has it (DLT), otherwise by WhatsApp template."""
    return "sms" if "sms" in getattr(services.get("comms"), "channels", {"sms"}) else "whatsapp"


def amounts_in_text(message: str) -> list[Money]:
    out = []
    for m in _AMOUNT_RE.findall(message):
        try:
            out.append(Money.of(m.replace(",", "")))
        except Exception:  # unparseable figure → treated as a mismatch by the caller
            out.append(Money(-1))
    return out


# ---------------------------------------------------------------- inputs
class CustomerRef(BaseModel):
    customer_id: str


class PolicyCheckIn(BaseModel):
    customer_id: str
    action_kind: str
    purpose: str = "service"
    message_text: str | None = None


class DebitRef(BaseModel):
    debit_id: str


class TemplateIn(BaseModel):
    key: str = Field(pattern=r"^[a-z_]+\.[a-z_]+$")
    language: str = Field(pattern=r"^[a-z]{2}$")


class PaymentLinkIn(BaseModel):
    debit_id: str
    attempt: int = Field(ge=1, le=10)


class WhatsAppIn(BaseModel):
    customer_id: str
    debit_id: str
    text: str = Field(min_length=10, max_length=1000)
    purpose: str = "recovery"


class ExposureIn(BaseModel):
    customer_id: str
    arm: str
    action_ref: str | None = None


class CreditDrawIn(BaseModel):
    shortfall_day: str
    reason: str = Field(max_length=500)


def h_credit_draw(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Records the approved draw request for the treasury team / bank integration. The amount is computed by the
    Treasury Agent's deterministic plan and carried in the approved params — the LLM never sets it."""
    assert isinstance(a, CreditDrawIn)
    return {"provider_ref": new_id("drw"), "status": "requested", "shortfall_day": a.shortfall_day}


class ReplyIn(BaseModel):
    customer_id: str
    debit_id: str
    intent: str
    promised_date: str | None = None


# ---------------------------------------------------------------- handlers
def h_get_profile(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, CustomerRef)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cust = _customer(c, ctx.tenant_id, a.customer_id)
        first_name = (cust.display_name or "").split(" ")[0] or "there"
        return {"customer_id": cust.customer_id, "first_name": first_name, "segment": cust.segment,
                "language": cust.preferred_language,
                "consents": _consents(cust.consents), "contacts_last_7d": _contacts_7d(c, ctx.tenant_id,
                                                                                        a.customer_id, ctx.now)}


def h_policy_check(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, PolicyCheckIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = evaluate(contact_request(c, ctx, a.customer_id, a.action_kind, a.purpose, a.message_text),
                     ctx.services.get("policy_config", settings.policy_config)(c, ctx.tenant_id))
    return {"outcome": d.outcome.value, "policy_ids": [h.policy_id for h in d.hits], "policy_version": d.policy_version,
            "messages": [h.message for h in d.hits],
            "retry_after": d.retry_after.isoformat() if d.retry_after else None}


def h_create_link(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, PaymentLinkIn)
    provider: PaymentProvider = ctx.services["provider"]
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
    if d.status == "succeeded":
        raise ValueError("debit already paid; refusing to create a payment link")
    link = provider.create_payment_link(LinkRequest(
        amount=Money(d.amount_minor, d.currency), reference_id=f"{d.debit_id}.{a.attempt}",
        description=f"Payment for {d.scheduled_for:%d %b %Y}", customer_name=None, customer_phone=None,
        customer_email=None, expire_by=ctx.now + timedelta(days=7)))
    return {"provider_ref": link.link_id, "url": link.url, "amount_minor": link.amount.minor,
            "reference_id": link.reference_id}


def h_send_whatsapp(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, WhatsAppIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
    due = Money(d.amount_minor, d.currency)
    mentioned = amounts_in_text(a.text)
    if any(m != due for m in mentioned):
        raise ValueError(f"message mentions {[str(m) for m in mentioned]} but the debit is {due}")
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cust = _customer(c, ctx.tenant_id, a.customer_id)
    link = _URL.search(a.text)
    tpl = OutboundTemplate("whatsapp.recovery", cust.preferred_language or "en",
                           {"name": first_name(cust.display_name), "amount": rupees(due), "plan": "subscription",
                            "link": link.group(0) if link else ""})
    mid = ctx.services["comms"].send("whatsapp", a.customer_id, a.text, ctx.now, tenant_id=ctx.tenant_id,
                                     template=tpl)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'whatsapp', :p, 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": a.customer_id, "p": a.purpose, "n": ctx.now})
    return {"provider_ref": mid, "channel": "whatsapp"}


def h_predebit_notice(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, DebitRef)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
        cust = _customer(c, ctx.tenant_id, d.customer_id)
        channel = notice_channel(ctx.services)
        lang = cust.preferred_language or "en"
        tpl = templates.resolve(c, ctx.tenant_id, f"{channel}.predebit_notice", lang)
    due = Money(d.amount_minor, d.currency)
    values = {"amount": rupees(due), "date": f"{d.scheduled_for:%d %b %Y}", "plan": "subscription"}
    body = tpl.render(**values)
    mid = ctx.services["comms"].send(channel, d.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                     template=OutboundTemplate(f"{channel}.predebit_notice", lang, values))
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, :ch, 'mandatory', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": d.customer_id, "ch": channel, "n": ctx.now})
        c.execute(text("UPDATE billing.debits SET status='notified', predebit_notified_at=:n "
                       "WHERE tenant_id=:t AND debit_id=:d AND status='scheduled'"),
                  {"n": ctx.now, "t": ctx.tenant_id, "d": a.debit_id})
    return {"provider_ref": mid, "channel": channel, "text": body, "template_ref": tpl.ref}


def h_get_template(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, TemplateIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        tpl = templates.resolve(c, ctx.tenant_id, a.key, a.language)
    return {"key": tpl.key, "language": tpl.language, "body": tpl.body, "version": tpl.version,
            "source": tpl.source, "ref": tpl.ref}


def h_assign(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, CustomerRef)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        arm = experiments.assign(c, ctx.tenant_id, ctx.services["experiment_id"], a.customer_id, ctx.now)
    return {"arm": arm, "experiment_id": ctx.services["experiment_id"]}


def h_exposure(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, ExposureIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        xid = experiments.log_exposure(c, ctx.tenant_id, ctx.services["experiment_id"], a.customer_id, a.arm,
                                       a.action_ref, ctx.now)
    return {"exposure_id": xid}


def h_verify_credit(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, DebitRef)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
    if not d.provider_payment_id:
        return {"verified": False, "reason": "no_provider_payment"}
    v = verify_capture(ctx.services["provider"], d.provider_payment_id, Money(d.amount_minor, d.currency))
    return {"verified": v.verified, "reason": v.reason, "provider_ref": v.provider_payment_id,
            "evidence_hash": v.evidence_hash}


def h_record_reply(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, ReplyIn)
    mem_id = new_id("mem")
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ai.memory (tenant_id, memory_id, scope, subject_id, key, value, source, "
                       "confidence, provenance, consent_basis) VALUES (:t, :m, 'episodic', :s, 'customer_reply', "
                       "CAST(:v AS jsonb), 'customer_message', :conf, CAST(:p AS jsonb), 'service_communication')"),
                  {"t": ctx.tenant_id, "m": mem_id, "s": a.customer_id,
                   "v": json.dumps({"intent": a.intent, "promised_date": a.promised_date, "debit_id": a.debit_id}),
                   "conf": 0.8, "p": json.dumps({"agent": ctx.agent_id, "case_id": ctx.case_id})})
    return {"memory_id": mem_id}


class AckIn(BaseModel):
    customer_id: str
    intent: str = Field(pattern=r"^(promise_to_pay|hardship|opt_out|cancel_request|other)$")
    promised_date: str | None = None
    via: str | None = None                  # "audio" → also answer with a spoken reply (Sarvam TTS)


ACK_KEY = {"hardship": "hardship", "opt_out": "optout"}


def h_acknowledge_reply(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Answer the customer's message in their language, inside the WhatsApp service window: what we understood and
    what happens next. Never promotional, never new facts beyond the promised date they gave."""
    assert isinstance(a, AckIn)
    from datetime import date

    from nirantar.comms.sink import ChannelNotConnected

    key = "promise" if a.intent == "promise_to_pay" and a.promised_date else ACK_KEY.get(a.intent, "other")
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cust = _customer(c, ctx.tenant_id, a.customer_id)
        tpl = templates.resolve(c, ctx.tenant_id, f"whatsapp.reply_ack_{key}", cust.preferred_language or "en")
    values = {"name": first_name(cust.display_name)}
    if key == "promise" and a.promised_date:
        values["date"] = f"{date.fromisoformat(a.promised_date[:10]):%d %b %Y}"
    body = tpl.render(**values)
    comms = ctx.services["comms"]
    mid = comms.send("whatsapp", a.customer_id, body, ctx.now, tenant_id=ctx.tenant_id)
    voice_ref, voice_error = None, None
    if a.via == "audio" and hasattr(comms, "send_voice"):
        try:
            voice_ref = comms.send_voice(ctx.tenant_id, a.customer_id, body, tpl.language, ctx.now)
        except ChannelNotConnected as exc:
            voice_error = str(exc)[:200]
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": a.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "template_ref": tpl.ref, "voice_ref": voice_ref, "voice_error": voice_error,
            "text": body}


def p_acknowledge(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, AckIn)
    kind = "optout_confirmation" if a.intent == "opt_out" else "reply_acknowledgement"
    return contact_request(c, ctx, a.customer_id, "send_whatsapp", "service", mandatory_kind=kind)


# ---------------------------------------------------------------- win-back (P4, ADR-0014)
class OfferIn(BaseModel):
    case_id: str = Field(pattern=r"^cas_[A-Za-z0-9_]+$")


class WinbackIn(BaseModel):
    case_id: str = Field(pattern=r"^cas_[A-Za-z0-9_]+$")
    text: str = Field(min_length=10, max_length=1000)
    offer: str = Field(default="", max_length=300)          # the offer line inside the text (template parameter)


def _revival_case(conn: Connection, tenant_id: str, case_id: str) -> dict[str, Any]:
    """The revival case written by candidate selection (code): subscription, customer, list amount, arm."""
    row = conn.execute(text("SELECT customer_id, subject_id, summary FROM ops.cases WHERE tenant_id=:t AND case_id=:c "
                            "AND kind='revival'"), {"t": tenant_id, "c": case_id}).one()
    summary = row.summary if isinstance(row.summary, dict) else json.loads(row.summary)
    return {**summary, "customer_id": row.customer_id, "entity_id": row.subject_id}


def offer_terms(list_amount_minor: int, discount_pct: int) -> int:
    """Offered amount = list amount minus the arm's percentage, rounded to whole rupees, never below Rs 1."""
    return max(100, round(list_amount_minor * (100 - discount_pct) / 100 / 100) * 100)


def h_create_offer(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, OfferIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        case = _revival_case(c, ctx.tenant_id, a.case_id)
        existing = c.execute(text("SELECT offer_id, link_ref, offer_amount_minor, discount_pct FROM billing.offers "
                                  "WHERE tenant_id=:t AND case_id=:c"), {"t": ctx.tenant_id, "c": a.case_id}).first()
    if existing is not None and existing.link_ref:                     # idempotent per case
        return {"offer_id": existing.offer_id, "url": existing.link_ref,
                "offer_amount_minor": existing.offer_amount_minor, "discount_pct": existing.discount_pct}
    list_amount, pct = int(case["list_amount_minor"]), int(case["discount_pct"])
    offer_amount = offer_terms(list_amount, pct)
    offer_id = existing.offer_id if existing else new_id("ofr")
    link = ctx.services["provider"].create_payment_link(LinkRequest(
        Money(offer_amount), offer_id, f"Restart {case.get('plan') or 'your subscription'}", None, None, None,
        expire_by=ctx.now + timedelta(days=int(case["window_days"]))))
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO billing.offers (tenant_id, offer_id, case_id, experiment_id, subscription_id, "
                       "customer_id, arm, kind, discount_pct, list_amount_minor, offer_amount_minor, link_ref, status, "
                       "created_at, expires_at) VALUES (:t, :o, :c, :e, :s, :cu, :arm, :k, :pct, :la, :oa, :url, "
                       "'created', :now, :exp) ON CONFLICT (tenant_id, case_id) DO UPDATE SET "
                       "link_ref=EXCLUDED.link_ref"),
                  {"t": ctx.tenant_id, "o": offer_id, "c": a.case_id, "e": case["experiment_id"],
                   "s": case["entity_id"], "cu": case["customer_id"], "arm": case["arm"], "k": case["kind"],
                   "pct": pct, "la": list_amount, "oa": offer_amount, "url": link.url, "now": ctx.now,
                   "exp": ctx.now + timedelta(days=int(case["window_days"]))})
    return {"offer_id": offer_id, "url": link.url, "offer_amount_minor": offer_amount, "discount_pct": pct,
            "provider_ref": link.link_id}


def h_send_winback(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    assert isinstance(a, WinbackIn)
    if amounts_in_text(a.text):
        raise ValueError("win-back messages must not state amounts; the payment link carries the price")
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        case = _revival_case(c, ctx.tenant_id, a.case_id)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cust = _customer(c, ctx.tenant_id, case["customer_id"])
    link = _URL.search(a.text)
    tpl = OutboundTemplate("whatsapp.winback", cust.preferred_language or "en",
                           {"name": first_name(cust.display_name), "plan": case.get("plan") or "your subscription",
                            "offer": a.offer, "link": link.group(0) if link else ""})
    mid = ctx.services["comms"].send("whatsapp", case["customer_id"], a.text, ctx.now, tenant_id=ctx.tenant_id,
                                     template=tpl)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'whatsapp', 'promotional', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": case["customer_id"], "n": ctx.now})
        c.execute(text("UPDATE billing.offers SET status='sent' WHERE tenant_id=:t AND case_id=:c "
                       "AND status='created'"), {"t": ctx.tenant_id, "c": a.case_id})
    return {"provider_ref": mid, "channel": "whatsapp"}


# ---------------------------------------------------------------- disputes (P5, ADR-0015)
class DisputeCaseIn(BaseModel):
    case_id: str = Field(pattern=r"^cas_[A-Za-z0-9_]+$")


def _dispute_case(conn: Connection, tenant_id: str, case_id: str) -> dict[str, Any]:
    row = conn.execute(text("SELECT c.summary, d.dispute_id, d.provider_dispute_id, d.amount_minor, d.currency, "
                            "d.status, d.respond_by FROM ops.cases c JOIN billing.disputes d "
                            "ON d.tenant_id=c.tenant_id "
                            "AND d.dispute_id=c.subject_id WHERE c.tenant_id=:t AND c.case_id=:c AND c.kind='dispute'"),
                       {"t": tenant_id, "c": case_id}).one()
    summary = row.summary if isinstance(row.summary, dict) else json.loads(row.summary)
    return {**summary, "dispute_id": row.dispute_id, "provider_dispute_id": row.provider_dispute_id,
            "amount_minor": int(row.amount_minor), "currency": row.currency, "status": row.status}


def h_submit_representment(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Upload the stored evidence pack (Documents API) and contest the dispute with it (action=submit)."""
    assert isinstance(a, DisputeCaseIn)
    from nirantar.evidence import objectstore

    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        case = _dispute_case(c, ctx.tenant_id, a.case_id)
    if case["status"] == "submitted":
        return {"status": "already_submitted", "provider_ref": case["provider_dispute_id"]}
    provider = ctx.services["provider"]
    pdf = objectstore.get(case["pack_uri"])
    doc_id = provider.upload_document(f"evidence-{case['provider_dispute_id']}.pdf", pdf, "application/pdf")
    result = provider.contest_dispute(case["provider_dispute_id"], summary=case["summary"],
                                      document_ids={"proof_of_service": [doc_id]}, submit=True)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("UPDATE billing.disputes SET status='submitted', updated_at=:n WHERE tenant_id=:t AND "
                       "dispute_id=:d"), {"n": ctx.now, "t": ctx.tenant_id, "d": case["dispute_id"]})
    return {"status": result.status, "provider_ref": case["provider_dispute_id"], "document_id": doc_id}


# ---------------------------------------------------------------- mandates (P5, ADR-0015)
class MandateCaseIn(BaseModel):
    case_id: str = Field(pattern=r"^cas_[A-Za-z0-9_]+$")


def _mandate_case(conn: Connection, tenant_id: str, case_id: str) -> dict[str, Any]:
    row = conn.execute(text("SELECT customer_id, summary FROM ops.cases WHERE tenant_id=:t AND case_id=:c AND "
                            "kind='mandate'"), {"t": tenant_id, "c": case_id}).one()
    summary = row.summary if isinstance(row.summary, dict) else json.loads(row.summary)
    return {**summary, "customer_id": row.customer_id}


def h_mandate_repair(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Ask the customer to fix their mandate: a provider re-authorisation link (the customer authorises it
    themselves), or — for a paused UPI AutoPay — a request to resume it in their UPI app."""
    assert isinstance(a, MandateCaseIn)
    from datetime import date

    from nirantar.core import crypto
    from nirantar.settings.schema import Operations

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        case = _mandate_case(c, ctx.tenant_id, a.case_id)
        if case.get("sent_ref"):
            return {"status": "already_sent", "provider_ref": case["sent_ref"]}
        cust = c.execute(text("SELECT display_name, phone_enc, email_enc, preferred_language FROM billing.customers "
                              "WHERE tenant_id=:t AND customer_id=:c"), {"t": ctx.tenant_id, "c": case["customer_id"]}
                         ).one()
        mandate = c.execute(text("SELECT provider_customer_ref FROM billing.mandates WHERE tenant_id=:t AND "
                                 "mandate_id=:m"), {"t": ctx.tenant_id, "m": case["mandate_id"]}).one()
        cfg = settings.model(c, ctx.tenant_id, "operations", Operations).mandate_health
        key = "whatsapp.mandate_resume" if case["repair"] == "resume_paused" else "whatsapp.mandate_reauth"
        tpl = templates.resolve(c, ctx.tenant_id, key, cust.preferred_language or "en")
    when = f"{date.fromisoformat(case['next_debit_on']):%d %b %Y}"
    values = {"name": cust.display_name or "", "plan": "subscription", "date": when}
    link: dict[str, Any] | None = None
    if key == "whatsapp.mandate_reauth":
        link = case.get("link")
        if link is None:                    # create once; a retried send reuses it (provider receipts are unique)
            create = getattr(ctx.services["provider"], "create_registration_link", None)
            if create is None:
                raise ValueError("this provider cannot create mandate re-authorisation links")
            limit = max(int(case["next_amount_minor"]), int(case.get("max_amount_minor") or 0))
            made = create(customer_ref=mandate.provider_customer_ref, name=cust.display_name or "Customer",
                          contact=crypto.decrypt(bytes(cust.phone_enc), ctx.tenant_id) if cust.phone_enc else None,
                          email=crypto.decrypt(bytes(cust.email_enc), ctx.tenant_id) if cust.email_enc else None,
                          rail=case["rail"], max_amount=Money(limit),
                          expire_at=ctx.now + timedelta(days=cfg.link_valid_days),
                          receipt=f"mdr_{case['problem_key']}", description="Renew auto-pay for your subscription")
            link = {"id": made.link_id, "url": made.url}
            with tenant_tx(ctx.tenant_id, engine) as c:
                c.execute(text("UPDATE ops.cases SET summary = summary || CAST(:s AS jsonb) WHERE tenant_id=:t AND "
                               "case_id=:c"), {"s": json.dumps({"link": link}), "t": ctx.tenant_id, "c": a.case_id})
        values["link"] = link["url"]
    body = tpl.render(**values)
    mid = ctx.services["comms"].send("whatsapp", case["customer_id"], body, ctx.now, tenant_id=ctx.tenant_id,
                                     template=OutboundTemplate(key, tpl.language, values))
    with tenant_tx(ctx.tenant_id, engine) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": case["customer_id"], "n": ctx.now})
        c.execute(text("UPDATE ops.cases SET summary = summary || CAST(:s AS jsonb), status='waiting' WHERE "
                       "tenant_id=:t AND case_id=:c"),
                  {"s": json.dumps({"sent_ref": mid, "template_ref": tpl.ref}), "t": ctx.tenant_id, "c": a.case_id})
    return {"provider_ref": mid, "channel": "whatsapp", "template_ref": tpl.ref,
            "link_id": link["id"] if link else None}


# ---------------------------------------------------------------- read tools for external MCP clients (P7)
class NoArgs(BaseModel):
    pass


class OpenCasesIn(BaseModel):
    kind: str | None = Field(default=None, pattern=r"^(debit_cycle|dispute|revival|mandate|collections)$")
    limit: int = Field(default=25, ge=1, le=100)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def h_overview(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """The same KPIs as the dashboard home page (system of record, no estimates)."""
    from nirantar.api import queries

    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        out: dict[str, Any] = _jsonable(queries.overview(c, ctx.now))
    return out


def h_open_cases(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Cases that are open, waiting for the customer, or escalated to a human — newest first."""
    assert isinstance(a, OpenCasesIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        rows = c.execute(text("SELECT case_id, kind, subject_id, customer_id, status, opened_at, "
                              "summary->>'reason' AS reason, summary->>'repair' AS repair, "
                              "summary->>'escalation' AS escalation FROM ops.cases WHERE status IN "
                              "('open','waiting','escalated') AND (CAST(:k AS text) IS NULL OR kind=:k) "
                              "ORDER BY opened_at DESC LIMIT :n"), {"k": a.kind, "n": a.limit}).all()
    return {"items": _jsonable([{k: v for k, v in r._mapping.items() if v is not None} for r in rows])}


# ---------------------------------------------------------------- customer-delegated requests (A2A, P7)
class CustomerAgentIn(BaseModel):
    customer_ref: str = Field(min_length=1, max_length=128)     # the merchant's own customer reference
    consent_ref: str = Field(min_length=4, max_length=256)      # the customer's delegation to their agent


class PauseIn(CustomerAgentIn):
    months: int = Field(ge=1, le=3)


def _customer_by_ref(conn: Connection, tenant_id: str, ref: str) -> str:
    cid = conn.execute(text("SELECT customer_id FROM billing.customers WHERE tenant_id=:t AND external_ref=:r"),
                       {"t": tenant_id, "r": ref}).scalar_one_or_none()
    if cid is None:
        raise ValueError("no customer with this reference")
    return str(cid)


def h_subscription_status(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """A customer's own agent asks about their subscriptions: plan amount, status and the next debit."""
    assert isinstance(a, CustomerAgentIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cid = _customer_by_ref(c, ctx.tenant_id, a.customer_ref)
        rows = c.execute(text(
            "SELECT s.subscription_id, s.status, s.amount_minor, s.currency, "
            "(SELECT min(d.scheduled_for) FROM billing.debits d WHERE d.tenant_id=s.tenant_id AND "
            " d.subscription_id=s.subscription_id AND d.status IN ('scheduled','notified')) AS next_debit_on "
            "FROM billing.subscriptions s WHERE s.tenant_id=:t AND s.customer_id=:c ORDER BY s.created_at"),
            {"t": ctx.tenant_id, "c": cid}).all()
    return {"subscriptions": [{"subscription_id": r.subscription_id, "status": r.status,
                               "amount": str(Money(int(r.amount_minor), r.currency)),
                               "next_debit_on": r.next_debit_on.isoformat() if r.next_debit_on else None}
                              for r in rows]}


def h_request_pause(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Pause the customer's active subscriptions at the provider and cancel debits scheduled inside the pause.
    Resuming at the end of the pause is a separate, explicit action (the provider does not auto-resume)."""
    assert isinstance(a, PauseIn)
    from datetime import date

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        cid = _customer_by_ref(c, ctx.tenant_id, a.customer_ref)
        subs = c.execute(text("SELECT subscription_id, provider_subscription_id FROM billing.subscriptions WHERE "
                              "tenant_id=:t AND customer_id=:c AND status='active'"),
                         {"t": ctx.tenant_id, "c": cid}).all()
    if not subs:
        raise ValueError("the customer has no active subscription")
    until = date.fromordinal(ctx.now.date().toordinal() + 30 * a.months)
    paused, cancelled = [], 0
    for s in subs:
        ctx.services["provider"].pause_subscription(s.provider_subscription_id)
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("UPDATE billing.subscriptions SET status='paused' WHERE tenant_id=:t AND "
                           "subscription_id=:s"), {"t": ctx.tenant_id, "s": s.subscription_id})
            cancelled += c.execute(text("UPDATE billing.debits SET status='cancelled', updated_at=:n WHERE "
                                        "tenant_id=:t AND subscription_id=:s AND status IN ('scheduled','notified') "
                                        "AND scheduled_for < :u"),
                                   {"n": ctx.now, "t": ctx.tenant_id, "s": s.subscription_id, "u": until}).rowcount
        paused.append(s.subscription_id)
    return {"paused": paused, "paused_until": until.isoformat(), "cancelled_debits": cancelled,
            "consent_ref": a.consent_ref}


def p_customer_request(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, CustomerAgentIn)
    cid = _customer_by_ref(c, ctx.tenant_id, a.customer_ref)
    cust = _customer(c, ctx.tenant_id, cid)
    return ActionRequest(tenant_id=ctx.tenant_id, action_kind="pause_subscription", segment=cust.segment,
                         now_utc=ctx.now, customer_timezone=cust.timezone, purpose="service")


# ---------------------------------------------------------------- policy builders
def p_representment(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """Representments above the tenant's threshold need a human (NIR-GOV-MAKER-CHECKER-001)."""
    assert isinstance(a, DisputeCaseIn)
    case = _dispute_case(c, ctx.tenant_id, a.case_id)
    return ActionRequest(tenant_id=ctx.tenant_id, action_kind="submit_representment", segment="subscription",
                         now_utc=ctx.now, customer_timezone="Asia/Kolkata", purpose="service",
                         amount_minor=case["amount_minor"])


def p_offer(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest | None:
    """Discounts are money: above the tenant's threshold they need a human (NIR-GOV-MAKER-CHECKER-001)."""
    assert isinstance(a, OfferIn)
    case = _revival_case(c, ctx.tenant_id, a.case_id)
    discount = int(case["list_amount_minor"]) - offer_terms(int(case["list_amount_minor"]), int(case["discount_pct"]))
    if discount <= 0:
        return None
    cust = _customer(c, ctx.tenant_id, case["customer_id"])
    return ActionRequest(tenant_id=ctx.tenant_id, action_kind="grant_discount", segment=cust.segment,
                         now_utc=ctx.now, customer_timezone=cust.timezone, purpose="promotional",
                         amount_minor=discount)


def p_winback(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """A win-back message is promotional (it invites a purchase and may carry an offer): it needs promotional
    consent (IN-TRAI-TCCCPR-DLT-001) on top of channel consent, contact window and fatigue rules."""
    assert isinstance(a, WinbackIn)
    case = _revival_case(c, ctx.tenant_id, a.case_id)
    req = contact_request(c, ctx, case["customer_id"], "send_whatsapp", "promotional", a.text)
    return ActionRequest(**{**req.__dict__, "contains_offer": True})
def p_mandate_repair(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """A mandate repair request is a service message about an existing subscription: channel consent, contact
    window, opt-out and fatigue rules apply (it is not promotional)."""
    assert isinstance(a, MandateCaseIn)
    case = _mandate_case(c, ctx.tenant_id, a.case_id)
    return contact_request(c, ctx, case["customer_id"], "send_whatsapp", "service")


def p_whatsapp(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, WhatsAppIn)
    return contact_request(c, ctx, a.customer_id, "send_whatsapp", a.purpose, a.text)


def p_predebit(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, DebitRef)
    d = _debit(c, ctx.tenant_id, a.debit_id)
    action = "send_sms" if notice_channel(ctx.services) == "sms" else "send_whatsapp"
    return contact_request(c, ctx, d.customer_id, action, "mandatory", mandatory_kind="predebit_notice")


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("customer.get_profile", "Segment, language, consents and recent contact count (no raw PII).",
         CustomerRef, h_get_profile, "read"),
    Tool("policy.check_action", "Dry-run the Compliance Guardian for a proposed contact.", PolicyCheckIn,
         h_policy_check, "read", records_decision=True),
    Tool("gateway.create_payment_link", "Create a payment link for a debit (amount comes from the debit).",
         PaymentLinkIn, h_create_link, "money"),
    Tool("comms.send_whatsapp", "Send a WhatsApp message about a debit; ₹ amounts must match the debit.",
         WhatsAppIn, h_send_whatsapp, "write", policy=p_whatsapp),
    Tool("comms.send_predebit_notice", "Send the mandatory pre-debit notice (deterministic template).", DebitRef,
         h_predebit_notice, "write", policy=p_predebit),
    Tool("content.get_template", "The tenant's approved wording for a message key and language (placeholders "
         "unfilled).", TemplateIn, h_get_template, "read"),
    Tool("experiment.assign_treatment", "Stable randomized arm for a customer.", CustomerRef, h_assign, "read"),
    Tool("experiment.log_exposure", "Record that a customer was exposed to an arm.", ExposureIn, h_exposure, "write"),
    Tool("ledger.verify_credit", "Verify a debit's payment against the provider.", DebitRef, h_verify_credit, "read"),
    Tool("case.record_reply", "Store a classified customer reply in episodic memory.", ReplyIn, h_record_reply,
         "write"),
    Tool("retention.create_offer", "Create the win-back offer and its payment link for a revival case (amount and "
         "discount come from the case, never from the caller).", OfferIn, h_create_offer, "money", policy=p_offer),
    Tool("comms.send_winback", "Send the win-back WhatsApp message for a revival case (promotional).", WinbackIn,
         h_send_winback, "write", policy=p_winback),
    Tool("dispute.submit_representment", "Upload the evidence pack and contest a dispute (amount from the dispute "
         "record).", DisputeCaseIn, h_submit_representment, "money", policy=p_representment),
    Tool("mandate.send_repair", "Send the customer a mandate repair request (re-authorisation link or resume "
         "instructions) for a mandate case.", MandateCaseIn, h_mandate_repair, "write", policy=p_mandate_repair),
    Tool("comms.acknowledge_reply", "Acknowledge the customer's message in their language (text, plus a spoken "
         "reply when they sent a voice note).", AckIn, h_acknowledge_reply, "write", policy=p_acknowledge),
    Tool("insights.overview", "Business overview: debits, recovery, failures, approvals and experiment status.",
         NoArgs, h_overview, "read"),
    Tool("cases.list_open", "Open cases (debit cycles, disputes, revivals, mandates) needing attention.", OpenCasesIn,
         h_open_cases, "read"),
    Tool("subscription.status_for_customer", "A customer's subscriptions, amounts and next debit (customer-delegated "
         "request; needs the customer's consent reference).", CustomerAgentIn, h_subscription_status, "read"),
    Tool("subscription.request_pause", "Pause a customer's active subscriptions for 1-3 months and cancel debits "
         "inside the pause (customer-delegated).", PauseIn, h_request_pause, "write", policy=p_customer_request),
    Tool("treasury.request_credit_draw", "Request a credit-line draw to cover a projected shortfall (always human-"
         "approved).", CreditDrawIn, h_credit_draw, "money", approval="always"),
)}

AGENT_SCOPES: dict[str, frozenset[str]] = {
    "conductor": frozenset({"customer.get_profile", "policy.check_action", "experiment.assign_treatment",
                            "experiment.log_exposure", "case.record_reply", "comms.acknowledge_reply"}),
    "debit_strategist": frozenset({"comms.send_predebit_notice", "customer.get_profile"}),
    "conversation_agent": frozenset({"comms.send_whatsapp", "gateway.create_payment_link", "customer.get_profile",
                                     "content.get_template"}),
    "verifier": frozenset({"ledger.verify_credit"}),
    "treasury_agent": frozenset({"treasury.request_credit_draw"}),
    "dispute_defender": frozenset({"dispute.submit_representment"}),
    "mandate_doctor": frozenset({"mandate.send_repair", "customer.get_profile"}),
    "revival_agent": frozenset({"content.get_template", "customer.get_profile", "retention.create_offer",
                                "comms.send_winback", "experiment.assign_treatment", "experiment.log_exposure"}),
}
