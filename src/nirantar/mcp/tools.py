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
        consents={k: v for k, v in consents.items() if isinstance(v, bool)},   # flags only, never evidence
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
    quote: str | None = Field(default=None, max_length=300)        # the customer's words (already OTP-redacted)
    source: str = Field(default="whatsapp_text", pattern=r"^(whatsapp_text|voice_note|operator)$")


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
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:     # every link for a debit is tracked and polled
        c.execute(text("INSERT INTO billing.payment_requests (tenant_id, request_id, debit_id, customer_id, provider, "
                       "provider_link_id, url, amount_minor, status, created_at) VALUES (:t, :r, :d, :c, :p, :l, :u, "
                       ":a, 'created', :n) ON CONFLICT (tenant_id, provider, provider_link_id) DO NOTHING"),
                  {"t": ctx.tenant_id, "r": new_id("prq"), "d": d.debit_id, "c": d.customer_id, "p": provider.name,
                   "l": link.link_id, "u": link.url, "a": link.amount.minor, "n": ctx.now})
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
        promise_id = None
        if a.intent == "promise_to_pay":            # promise tracker (P10): the newest promise supersedes older ones
            c.execute(text("UPDATE ops.promises SET status='superseded', resolved_at=:n WHERE tenant_id=:t AND "
                           "debit_id=:d AND status='open'"), {"n": ctx.now, "t": ctx.tenant_id, "d": a.debit_id})
            promise_id = new_id("prm")
            c.execute(text("INSERT INTO ops.promises (tenant_id, promise_id, debit_id, customer_id, promised_date, "
                           "source, quote, status, created_at) VALUES (:t, :p, :d, :c, :pd, :s, :q, 'open', :n)"),
                      {"t": ctx.tenant_id, "p": promise_id, "d": a.debit_id, "c": a.customer_id,
                       "pd": a.promised_date[:10] if a.promised_date else None, "s": a.source,
                       "q": (a.quote or "")[:300] or None, "n": ctx.now})
    return {"memory_id": mem_id, "promise_id": promise_id}


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


# ---------------------------------------------------------------- pay-by-link collection (P8.6, ADR-0022)
class PaymentRequestIn(BaseModel):
    debit_id: str
    occasion: str = Field(default="due", pattern=r"^(due|promise|incident)$")   # due date / promised day / outage


def h_payment_request(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Collect a debit by payment link: reuse the open link for this debit or create one (amount from the debit),
    record it, and send the due-date message in the customer's language. If no messaging channel is connected the
    link still exists and is returned, so the business can share it; the result says the message was not sent."""
    assert isinstance(a, PaymentRequestIn)
    from nirantar.comms.sink import ChannelNotConnected

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
        if d.status in ("succeeded", "cancelled"):
            raise ValueError(f"debit is {d.status}; refusing to request payment")
        cust = _customer(c, ctx.tenant_id, d.customer_id)
        plan: str = c.execute(text(
            "SELECT coalesce(p.name, 'subscription') FROM billing.debits x JOIN billing.subscriptions s ON "
            "s.tenant_id=x.tenant_id AND s.subscription_id=x.subscription_id LEFT JOIN billing.plans p ON "
            "p.tenant_id=s.tenant_id AND p.plan_id=s.plan_id WHERE x.tenant_id=:t AND x.debit_id=:d"),
            {"t": ctx.tenant_id, "d": a.debit_id}).scalar_one()
        open_req = c.execute(text(
            "SELECT request_id, provider_link_id, url FROM billing.payment_requests WHERE tenant_id=:t AND "
            "debit_id=:d AND status IN ('created','sent') ORDER BY created_at DESC LIMIT 1"),
            {"t": ctx.tenant_id, "d": a.debit_id}).first()
        n = int(c.execute(text("SELECT count(*) FROM billing.payment_requests WHERE tenant_id=:t AND debit_id=:d"),
                          {"t": ctx.tenant_id, "d": a.debit_id}).scalar_one())
        lang = cust.preferred_language or "en"
        key = {"promise": "whatsapp.promise_reminder", "incident": "whatsapp.bank_issue_retry"}.get(
            a.occasion, "whatsapp.payment_due")
        tpl = templates.resolve(c, ctx.tenant_id, key, lang)
    due = Money(d.amount_minor, d.currency)
    if open_req is not None:
        request_id, link_id, url = open_req.request_id, open_req.provider_link_id, open_req.url
    else:
        from nirantar.billing import checkout

        provider: PaymentProvider = ctx.services["provider"]
        request_id = new_id("prq")
        if checkout.supports_checkout(provider):
            # Nirantar pay page backed by a provider order: branded, no link quota, signature-confirmed
            order = provider.create_order(due, f"{d.debit_id}.c{n + 1}",       # type: ignore[attr-defined]
                                          {"nirantar_ref": d.debit_id, "request_id": request_id})
            kind, link_id = "checkout", order.order_id
            url = f"{checkout.public_base()}/pay/{checkout.token_for(ctx.tenant_id, request_id)}"
        else:
            link = provider.create_payment_link(LinkRequest(
                amount=due, reference_id=f"{d.debit_id}.p{n + 1}",
                description=f"{plan} - due {d.scheduled_for:%d %b %Y}", customer_name=None, customer_phone=None,
                customer_email=None, expire_by=ctx.now + timedelta(days=14)))
            kind, link_id, url = "link", link.link_id, link.url
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("INSERT INTO billing.payment_requests (tenant_id, request_id, debit_id, customer_id, "
                           "provider, provider_link_id, url, amount_minor, status, created_at, kind) VALUES (:t, :r, "
                           ":d, :c, :p, :l, :u, :a, 'created', :n, :k) ON CONFLICT (tenant_id, provider, "
                           "provider_link_id) DO NOTHING"),
                      {"t": ctx.tenant_id, "r": request_id, "d": d.debit_id, "c": d.customer_id, "p": provider.name,
                       "l": link_id, "u": url, "a": due.minor, "n": ctx.now, "k": kind})
    values = {"name": first_name(cust.display_name), "plan": plan, "amount": rupees(due),
              "date": f"{d.scheduled_for:%d %b %Y}", "link": url}
    body = tpl.render(**values)
    mid, error = None, None
    try:
        mid = ctx.services["comms"].send("whatsapp", d.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                         template=OutboundTemplate(key, lang, values))
    except ChannelNotConnected as exc:
        error = str(exc)[:200]
    with tenant_tx(ctx.tenant_id, engine) as c:
        if mid is not None:
            c.execute(text("UPDATE billing.payment_requests SET status='sent', sent_at=:n, channel_message_id=:m "
                           "WHERE tenant_id=:t AND request_id=:r AND status='created'"),
                      {"n": ctx.now, "m": mid, "t": ctx.tenant_id, "r": request_id})
            c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, "
                           "at) VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                      {"t": ctx.tenant_id, "i": new_id("cnt"), "c": d.customer_id, "n": ctx.now})
    return {"provider_ref": link_id, "request_id": request_id, "url": url, "sent": mid is not None,
            "message_id": mid, "channel_error": error, "text": body, "template_ref": tpl.ref}


def p_payment_request(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """A bill the customer signed up for is a service message: consent, contact window, opt-out and fatigue apply.
    On the day the customer themselves promised, the reminder is customer-requested: no fatigue budget."""
    assert isinstance(a, PaymentRequestIn)
    d = _debit(c, ctx.tenant_id, a.debit_id)
    req = contact_request(c, ctx, d.customer_id, "send_whatsapp", "service")
    if a.occasion == "promise":
        return ActionRequest(**{**req.__dict__, "customer_requested": True})
    return req


def h_receipt(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Post-debit notification: only for a debit the verifier settled, with the provider's payment reference."""
    assert isinstance(a, PaymentRequestIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        d = _debit(c, ctx.tenant_id, a.debit_id)
        if d.status != "succeeded" or not d.provider_payment_id:
            raise ValueError("no verified payment for this debit; refusing to send a receipt")
        cust = _customer(c, ctx.tenant_id, d.customer_id)
        plan: str = c.execute(text(
            "SELECT coalesce(p.name, 'subscription') FROM billing.debits x JOIN billing.subscriptions s ON "
            "s.tenant_id=x.tenant_id AND s.subscription_id=x.subscription_id LEFT JOIN billing.plans p ON "
            "p.tenant_id=s.tenant_id AND p.plan_id=s.plan_id WHERE x.tenant_id=:t AND x.debit_id=:d"),
            {"t": ctx.tenant_id, "d": a.debit_id}).scalar_one()
        lang = cust.preferred_language or "en"
        tpl = templates.resolve(c, ctx.tenant_id, "whatsapp.payment_receipt", lang)
    values = {"name": first_name(cust.display_name), "amount": rupees(Money(d.amount_minor, d.currency)),
              "plan": plan, "date": f"{ctx.now:%d %b %Y}", "ref": str(d.provider_payment_id)}
    body = tpl.render(**values)
    mid = ctx.services["comms"].send("whatsapp", d.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                     template=OutboundTemplate("whatsapp.payment_receipt", lang, values))
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, "
                       "at) VALUES (:t, :i, :c, 'whatsapp', 'mandatory', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": d.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "text": body, "template_ref": tpl.ref}


def p_receipt(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, PaymentRequestIn)
    d = _debit(c, ctx.tenant_id, a.debit_id)
    return contact_request(c, ctx, d.customer_id, "send_whatsapp", "mandatory", mandatory_kind="postdebit_notice")


# ---------------------------------------------------------------- B2B receivables (P10, ADR-0025)
class InvoiceStepIn(BaseModel):
    invoice_id: str = Field(pattern=r"^ivc_[0-9A-Z]{26}$")
    step: str = Field(pattern=r"^(reminder|due|overdue_1|overdue_2|final|manual)$")


_INVOICE_TEMPLATE = {"reminder": "whatsapp.invoice_reminder", "due": "whatsapp.invoice_reminder",
                     "manual": "whatsapp.invoice_reminder", "overdue_1": "whatsapp.invoice_overdue",
                     "overdue_2": "whatsapp.invoice_overdue", "final": "whatsapp.invoice_final"}


def h_invoice_request(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Ask for the OUTSTANDING amount of an invoice: reuse its open link / pay page or create one, record it, send the
    step's message in the customer's language. Channel unavailable → the link still exists and is returned."""
    assert isinstance(a, InvoiceStepIn)
    from nirantar.billing import checkout
    from nirantar.comms.sink import ChannelNotConnected

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        inv = c.execute(text("SELECT i.*, t.name AS business FROM billing.invoices i JOIN core.tenants t ON "
                             "t.tenant_id=i.tenant_id WHERE i.tenant_id=:t AND i.invoice_id=:i"),
                        {"t": ctx.tenant_id, "i": a.invoice_id}).one()
        if inv.status not in ("open", "partially_paid"):
            raise ValueError(f"invoice is {inv.status}; refusing to request payment")
        cust = _customer(c, ctx.tenant_id, inv.customer_id)
        owed = Money(int(inv.amount_minor) - int(inv.paid_minor), inv.currency)
        open_req = c.execute(text(
            "SELECT request_id, provider_link_id, url FROM billing.payment_requests WHERE tenant_id=:t AND "
            "invoice_id=:i AND status IN ('created','sent') AND amount_minor=:a ORDER BY created_at DESC LIMIT 1"),
            {"t": ctx.tenant_id, "i": a.invoice_id, "a": owed.minor}).first()
        n = int(c.execute(text("SELECT count(*) FROM billing.payment_requests WHERE tenant_id=:t AND invoice_id=:i"),
                          {"t": ctx.tenant_id, "i": a.invoice_id}).scalar_one())
        lang = cust.preferred_language or "en"
        tpl = templates.resolve(c, ctx.tenant_id, _INVOICE_TEMPLATE[a.step], lang)
    if open_req is not None:
        request_id, url = open_req.request_id, open_req.url
    else:
        provider: PaymentProvider = ctx.services["provider"]
        request_id = new_id("prq")
        if checkout.supports_checkout(provider):
            order = provider.create_order(owed, f"{a.invoice_id}.{n + 1}",          # type: ignore[attr-defined]
                                          {"nirantar_ref": a.invoice_id, "request_id": request_id})
            kind, link_id = "checkout", order.order_id
            url = f"{checkout.public_base()}/pay/{checkout.token_for(ctx.tenant_id, request_id)}"
        else:
            link = provider.create_payment_link(LinkRequest(
                amount=owed, reference_id=f"{a.invoice_id}.{n + 1}", description=f"Invoice {inv.number}",
                customer_name=None, customer_phone=None, customer_email=None, expire_by=ctx.now + timedelta(days=30)))
            kind, link_id, url = "link", link.link_id, link.url
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("INSERT INTO billing.payment_requests (tenant_id, request_id, invoice_id, customer_id, "
                           "provider, provider_link_id, url, amount_minor, status, created_at, kind) VALUES (:t, :r, "
                           ":i, :c, :p, :l, :u, :a, 'created', :n, :k) ON CONFLICT (tenant_id, provider, "
                           "provider_link_id) DO NOTHING"),
                      {"t": ctx.tenant_id, "r": request_id, "i": a.invoice_id, "c": inv.customer_id,
                       "p": provider.name, "l": link_id, "u": url, "a": owed.minor, "n": ctx.now, "k": kind})
    values = {"name": first_name(cust.display_name), "business": inv.business, "number": inv.number,
              "amount": rupees(owed), "date": f"{inv.due_on:%d %b %Y}",
              "deadline": f"{ctx.now + timedelta(days=7):%d %b %Y}", "link": url}
    body = tpl.render(**values)
    mid, error = None, None
    try:
        mid = ctx.services["comms"].send("whatsapp", inv.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                         template=OutboundTemplate(_INVOICE_TEMPLATE[a.step], lang, values))
    except ChannelNotConnected as exc:
        error = str(exc)[:200]
    if mid is not None:
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("UPDATE billing.payment_requests SET status='sent', sent_at=:n, channel_message_id=:m "
                           "WHERE tenant_id=:t AND request_id=:r AND status='created'"),
                      {"n": ctx.now, "m": mid, "t": ctx.tenant_id, "r": request_id})
            c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, "
                           "at) VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                      {"t": ctx.tenant_id, "i": new_id("cnt"), "c": inv.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "request_id": request_id, "url": url, "sent": mid is not None, "text": body,
            "channel_error": error, "template_ref": tpl.ref, "owed_minor": owed.minor}


def p_invoice_request(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """An invoice for goods/services delivered is a service message: consent, window, opt-out and fatigue apply."""
    assert isinstance(a, InvoiceStepIn)
    cust: str = c.execute(text("SELECT customer_id FROM billing.invoices WHERE tenant_id=:t AND invoice_id=:i"),
                     {"t": ctx.tenant_id, "i": a.invoice_id}).scalar_one()
    return contact_request(c, ctx, cust, "send_whatsapp", "service")


# ---------------------------------------------------------------- live voice (P11, ADR-0026)
class CallIn(BaseModel):
    customer_id: str
    debit_id: str


def h_place_call(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Place an outbound recovery call through Exotel. The call carries only a signed token; the voice gateway says
    the recording disclosure first and resolves the business, amount and language server-side."""
    assert isinstance(a, CallIn)
    import os

    from nirantar.core import crypto
    from nirantar.voice import calls

    exotel = ctx.services.get("voice")
    if exotel is None:
        raise ValueError("voice calls are not configured for this deployment (Exotel)")
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        cust = c.execute(text("SELECT phone_enc, preferred_language FROM billing.customers WHERE customer_id=:c"),
                         {"c": a.customer_id}).one()
    if cust.phone_enc is None:
        raise ValueError("customer has no phone number on file")
    phone = crypto.decrypt(bytes(cust.phone_enc), ctx.tenant_id)
    call_id = calls.create(ctx.services["engine"], ctx.tenant_id, a.customer_id, a.debit_id,
                           cust.preferred_language or "en", ctx.now)
    base = os.environ.get("NIRANTAR_PUBLIC_APP_URL", "").rstrip("/")
    callback = f"{base}/webhooks/exotel/{ctx.tenant_id}" if base else None
    placed = exotel.place_call(phone, calls.token_for(ctx.tenant_id, call_id), callback)
    calls.set_status(ctx.services["engine"], ctx.tenant_id, call_id, placed.status, call_sid=placed.call_sid,
                     now=ctx.now)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'voice', 'recovery', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": a.customer_id, "n": ctx.now})
    return {"provider_ref": placed.call_sid, "call_id": call_id, "status": placed.status}


def p_place_call(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    """Voice: voice consent, opt-out, contact window, fatigue, and a registered calling header (TRAI)."""
    assert isinstance(a, CallIn)
    return contact_request(c, ctx, a.customer_id, "voice_call", "recovery")


class StatementIn(BaseModel):
    customer_id: str = Field(pattern=r"^cus_[0-9A-Z]{26}$")


def h_invoice_statement(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """One message for all of a customer's open invoices, with one link for the total owed; a payment is allocated
    oldest-due-first. Reuses an open statement for the same invoices and amount."""
    assert isinstance(a, StatementIn)
    from nirantar.billing import checkout
    from nirantar.comms.sink import ChannelNotConnected

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        invs = c.execute(text("SELECT invoice_id, number, due_on, amount_minor - paid_minor AS owed FROM "
                              "billing.invoices WHERE tenant_id=:t AND customer_id=:c AND status IN "
                              "('open','partially_paid') ORDER BY due_on, number"),
                         {"t": ctx.tenant_id, "c": a.customer_id}).all()
        if not invs:
            raise ValueError("no open invoices for this customer")
        ids = [r.invoice_id for r in invs]
        owed = Money(sum(int(r.owed) for r in invs), "INR")
        business: str = c.execute(text("SELECT name FROM core.tenants WHERE tenant_id=:t"),
                             {"t": ctx.tenant_id}).scalar_one()
        cust = _customer(c, ctx.tenant_id, a.customer_id)
        open_req = c.execute(text(
            "SELECT request_id, url FROM billing.payment_requests WHERE tenant_id=:t AND invoice_ids @> :ids AND "
            "invoice_ids <@ :ids AND amount_minor=:a AND status IN ('created','sent') ORDER BY created_at DESC "
            "LIMIT 1"), {"t": ctx.tenant_id, "ids": ids, "a": owed.minor}).first()
        lang = cust.preferred_language or "en"
        tpl = templates.resolve(c, ctx.tenant_id, "whatsapp.invoice_statement", lang)
    if open_req is not None:
        request_id, url = open_req.request_id, open_req.url
    else:
        provider: PaymentProvider = ctx.services["provider"]
        request_id = new_id("prq")
        if checkout.supports_checkout(provider):
            order = provider.create_order(owed, request_id,                       # type: ignore[attr-defined]
                                          {"nirantar_ref": request_id})
            kind, link_id = "checkout", order.order_id
            url = f"{checkout.public_base()}/pay/{checkout.token_for(ctx.tenant_id, request_id)}"
        else:
            link = provider.create_payment_link(LinkRequest(
                amount=owed, reference_id=request_id, description=f"Statement: {len(ids)} invoices",
                customer_name=None, customer_phone=None, customer_email=None, expire_by=ctx.now + timedelta(days=30)))
            kind, link_id, url = "link", link.link_id, link.url
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("INSERT INTO billing.payment_requests (tenant_id, request_id, invoice_ids, customer_id, "
                           "provider, provider_link_id, url, amount_minor, status, created_at, kind) VALUES (:t, :r, "
                           ":ids, :c, :p, :l, :u, :a, 'created', :n, :k)"),
                      {"t": ctx.tenant_id, "r": request_id, "ids": ids, "c": a.customer_id, "p": provider.name,
                       "l": link_id, "u": url, "a": owed.minor, "n": ctx.now, "k": kind})
    listing = "; ".join(f"{r.number} {rupees(Money(int(r.owed), 'INR'))} (due {r.due_on:%d %b})" for r in invs)
    values = {"name": first_name(cust.display_name), "business": business, "count": str(len(ids)),
              "amount": rupees(owed), "list": listing, "link": url}
    body = tpl.render(**values)
    mid, error = None, None
    try:
        mid = ctx.services["comms"].send("whatsapp", a.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                         template=OutboundTemplate("whatsapp.invoice_statement", lang, values))
    except ChannelNotConnected as exc:
        error = str(exc)[:200]
    if mid is not None:
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("UPDATE billing.payment_requests SET status='sent', sent_at=:n, channel_message_id=:m "
                           "WHERE tenant_id=:t AND request_id=:r AND status='created'"),
                      {"n": ctx.now, "m": mid, "t": ctx.tenant_id, "r": request_id})
            c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, "
                           "at) VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                      {"t": ctx.tenant_id, "i": new_id("cnt"), "c": a.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "request_id": request_id, "url": url, "sent": mid is not None, "text": body,
            "invoices": ids, "owed_minor": owed.minor, "channel_error": error}


def p_invoice_statement(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, StatementIn)
    return contact_request(c, ctx, a.customer_id, "send_whatsapp", "service")


class CheckoutStepIn(BaseModel):
    session_id: str = Field(pattern=r"^chk_[0-9A-Z]{26}$")
    step: str = Field(pattern=r"^(nudge|follow_up)$")


_CHECKOUT_TEMPLATE = {"bank_issue": "whatsapp.checkout_bank_issue", "payment_failed": "whatsapp.checkout_payment_retry",
                      "insufficient_funds": "whatsapp.checkout_payment_retry",
                      "card_problem": "whatsapp.checkout_payment_retry",
                      "limit_exceeded": "whatsapp.checkout_payment_retry",
                      "repeated_failures": "whatsapp.checkout_payment_retry"}


def _checkout_items(items: Any) -> str:
    rows = items if isinstance(items, list) else json.loads(items or "[]")
    names = [f"{r['qty']} x {r['name']}" if int(r.get("qty", 1)) > 1 else str(r["name"]) for r in rows[:3]]
    more = len(rows) - len(names)
    if not names:
        return "your items"
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def h_checkout_recovery(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """Remind a customer about a checkout they did not complete, with a fresh secure payment link for EXACTLY the
    checkout's amount (never a discount, never a different amount). Reuses an open link for the same checkout."""
    assert isinstance(a, CheckoutStepIn)
    from nirantar.billing import checkout as paypage
    from nirantar.comms.sink import ChannelNotConnected

    engine = ctx.services["engine"]
    with tenant_tx(ctx.tenant_id, engine) as c:
        s = c.execute(text("SELECT k.*, t.name AS business FROM billing.checkout_sessions k JOIN core.tenants t ON "
                           "t.tenant_id=k.tenant_id WHERE k.tenant_id=:t AND k.session_id=:s"),
                      {"t": ctx.tenant_id, "s": a.session_id}).one()
        if s.status != "open":
            raise ValueError(f"checkout is {s.status}; refusing to remind")
        if s.customer_id is None:
            raise ValueError("checkout has no customer to contact")
        cust = _customer(c, ctx.tenant_id, s.customer_id)
        amount = Money(int(s.amount_minor), s.currency)
        open_req = c.execute(text(
            "SELECT request_id, url FROM billing.payment_requests WHERE tenant_id=:t AND checkout_session_id=:s AND "
            "status IN ('created','sent') AND amount_minor=:a ORDER BY created_at DESC LIMIT 1"),
            {"t": ctx.tenant_id, "s": a.session_id, "a": amount.minor}).first()
        lang = cust.preferred_language or "en"
        key = "whatsapp.checkout_follow_up" if a.step == "follow_up" else _CHECKOUT_TEMPLATE.get(
            s.cause or "", "whatsapp.checkout_reminder")
        tpl = templates.resolve(c, ctx.tenant_id, key, lang)
    if open_req is not None:
        request_id, url = open_req.request_id, open_req.url
    else:
        provider: PaymentProvider = ctx.services["provider"]
        request_id = new_id("prq")
        if paypage.supports_checkout(provider):
            order = provider.create_order(amount, request_id,                       # type: ignore[attr-defined]
                                          {"nirantar_ref": request_id})
            kind, link_id = "checkout", order.order_id
            url = f"{paypage.public_base()}/pay/{paypage.token_for(ctx.tenant_id, request_id)}"
        else:
            link = provider.create_payment_link(LinkRequest(
                amount=amount, reference_id=request_id, description=f"Order {s.checkout_ref}"[:120],
                customer_name=None, customer_phone=None, customer_email=None, expire_by=ctx.now + timedelta(days=3)))
            kind, link_id, url = "link", link.link_id, link.url
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("INSERT INTO billing.payment_requests (tenant_id, request_id, checkout_session_id, "
                           "customer_id, provider, provider_link_id, url, amount_minor, status, created_at, kind) "
                           "VALUES (:t, :r, :s, :c, :p, :l, :u, :a, 'created', :n, :k)"),
                      {"t": ctx.tenant_id, "r": request_id, "s": a.session_id, "c": s.customer_id, "p": provider.name,
                       "l": link_id, "u": url, "a": amount.minor, "n": ctx.now, "k": kind})
    values = {"name": first_name(cust.display_name), "business": s.business, "amount": rupees(amount),
              "items": _checkout_items(s.items), "link": url}
    body = tpl.render(**values)
    mid, error = None, None
    try:
        mid = ctx.services["comms"].send("whatsapp", s.customer_id, body, ctx.now, tenant_id=ctx.tenant_id,
                                         template=OutboundTemplate(key, lang, values))
    except ChannelNotConnected as exc:
        error = str(exc)[:200]
    if mid is not None:
        with tenant_tx(ctx.tenant_id, engine) as c:
            c.execute(text("UPDATE billing.payment_requests SET status='sent', sent_at=:n, channel_message_id=:m "
                           "WHERE tenant_id=:t AND request_id=:r AND status='created'"),
                      {"n": ctx.now, "m": mid, "t": ctx.tenant_id, "r": request_id})
            c.execute(text("UPDATE billing.checkout_sessions SET first_contact_at=coalesce(first_contact_at, :n) "
                           "WHERE tenant_id=:t AND session_id=:s"),
                      {"n": ctx.now, "t": ctx.tenant_id, "s": a.session_id})
            c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, "
                           "at) VALUES (:t, :i, :c, 'whatsapp', 'promotional', 'accepted', :n)"),
                      {"t": ctx.tenant_id, "i": new_id("cnt"), "c": s.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "request_id": request_id, "url": url, "sent": mid is not None, "text": body,
            "template": key, "amount_minor": amount.minor, "channel_error": error}


def p_checkout_recovery(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest | None:
    assert isinstance(a, CheckoutStepIn)
    cust = c.execute(text("SELECT customer_id FROM billing.checkout_sessions WHERE tenant_id=:t AND session_id=:s"),
                     {"t": ctx.tenant_id, "s": a.session_id}).scalar_one_or_none()
    if cust is None:
        return None                                     # the handler refuses: nobody to contact
    # a cart reminder is marketing under WhatsApp's rules: WhatsApp AND promotional consent are both required
    return contact_request(c, ctx, cust, "send_whatsapp", "promotional")


# ---------------------------------------------------------------- human takeover (P8.5, ADR-0021)
class OperatorReplyIn(BaseModel):
    customer_id: str
    text: str = Field(min_length=2, max_length=1000)
    operator: str = Field(min_length=3, max_length=200)     # set by the API from the signed-in principal


def h_operator_reply(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    """A person answers the customer in the inbox. Free text is only possible inside WhatsApp's 24-hour service
    window (the channel refuses otherwise); any ₹ figure must equal one of the customer's unpaid debits."""
    assert isinstance(a, OperatorReplyIn)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        unpaid = {Money(int(r.amount_minor), r.currency) for r in c.execute(text(
            "SELECT amount_minor, currency FROM billing.debits WHERE tenant_id=:t AND customer_id=:c AND status IN "
            "('failed','scheduled','notified','attempting')"), {"t": ctx.tenant_id, "c": a.customer_id})}
    wrong = [str(m) for m in amounts_in_text(a.text) if m not in unpaid]
    if wrong:
        raise ValueError(f"message mentions {wrong}, which is not an amount this customer owes")
    mid = ctx.services["comms"].send("whatsapp", a.customer_id, a.text, ctx.now, tenant_id=ctx.tenant_id)
    with tenant_tx(ctx.tenant_id, ctx.services["engine"]) as c:
        c.execute(text("INSERT INTO ops.contacts (tenant_id, contact_id, customer_id, channel, purpose, status, at) "
                       "VALUES (:t, :i, :c, 'whatsapp', 'service', 'accepted', :n)"),
                  {"t": ctx.tenant_id, "i": new_id("cnt"), "c": a.customer_id, "n": ctx.now})
    return {"provider_ref": mid, "channel": "whatsapp", "operator": a.operator}


def p_operator_reply(c: Connection, ctx: ToolContext, a: BaseModel) -> ActionRequest:
    assert isinstance(a, OperatorReplyIn)
    return contact_request(c, ctx, a.customer_id, "send_whatsapp", "service", a.text,
                           mandatory_kind="reply_acknowledgement")


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
    Tool("billing.send_payment_request", "Collect a due debit by payment link: create (or reuse) the link and send "
         "the due-date message in the customer's language.", PaymentRequestIn, h_payment_request, "money",
         policy=p_payment_request),
    Tool("comms.send_receipt", "Send the payment receipt (post-debit notification) for a verified payment.",
         PaymentRequestIn, h_receipt, "write", policy=p_receipt),
    Tool("billing.send_invoice_request", "Ask a business customer to pay the outstanding amount of an invoice "
         "(reminder, due date, polite overdue follow-up).", InvoiceStepIn, h_invoice_request, "money",
         policy=p_invoice_request),
    Tool("billing.send_invoice_statement", "One message for all of a business customer's open invoices with one "
         "'pay all' link (allocated oldest-due-first).", StatementIn, h_invoice_statement, "money",
         policy=p_invoice_statement),
    Tool("billing.send_checkout_recovery", "Remind a customer about a checkout they did not complete, with a "
         "secure payment link for exactly the checkout's amount.", CheckoutStepIn, h_checkout_recovery, "money",
         policy=p_checkout_recovery),
    Tool("billing.send_final_notice", "Send the final notice for an overdue invoice (always approved by a person).",
         InvoiceStepIn, h_invoice_request, "money", policy=p_invoice_request, approval="always"),
    Tool("comms.place_call", "Place an outbound recovery call (Exotel, the customer's language; recording disclosed "
         "first).", CallIn, h_place_call, "write", policy=p_place_call),
    Tool("comms.operator_reply", "A person's reply to the customer inside the WhatsApp service window.",
         OperatorReplyIn, h_operator_reply, "write", idempotent=False, policy=p_operator_reply),
)}

AGENT_SCOPES: dict[str, frozenset[str]] = {
    "conductor": frozenset({"customer.get_profile", "policy.check_action", "experiment.assign_treatment",
                            "experiment.log_exposure", "case.record_reply", "comms.acknowledge_reply"}),
    "debit_strategist": frozenset({"comms.send_predebit_notice", "customer.get_profile"}),
    "conversation_agent": frozenset({"comms.send_whatsapp", "gateway.create_payment_link", "customer.get_profile",
                                     "comms.place_call",
                                     "content.get_template"}),
    "verifier": frozenset({"ledger.verify_credit"}),
    "treasury_agent": frozenset({"treasury.request_credit_draw"}),
    "dispute_defender": frozenset({"dispute.submit_representment"}),
    "mandate_doctor": frozenset({"mandate.send_repair", "customer.get_profile"}),
    "revival_agent": frozenset({"content.get_template", "customer.get_profile", "retention.create_offer",
                                "comms.send_winback", "experiment.assign_treatment", "experiment.log_exposure"}),
    # an operator-launched recovery batch (P8.5): the same tools the agents use, never more
    "recovery_batch": frozenset({"gateway.create_payment_link", "comms.send_whatsapp", "mandate.send_repair",
                                 "comms.send_predebit_notice", "customer.get_profile",
                                 "billing.send_payment_request"}),
    "human_operator": frozenset({"comms.operator_reply"}),
    "receivables_agent": frozenset({"billing.send_invoice_request", "billing.send_final_notice",
                                    "billing.send_invoice_statement",
                                    "customer.get_profile"}),
    "billing_agent": frozenset({"billing.send_payment_request", "comms.send_receipt", "customer.get_profile"}),
    "checkout_agent": frozenset({"billing.send_checkout_recovery", "customer.get_profile"}),
}
