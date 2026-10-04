"""The production comms sink: WhatsApp Cloud API now, SMS (DLT) on hold.

send() decides HOW a message may go out:
  * the customer wrote to us in the last 24 h (WhatsApp service window) → free text (the gateway-checked text);
  * otherwise → the Meta-APPROVED template for the registry key, with the same facts as parameters;
  * no approved template → refuse loudly (TemplateNotApproved); the gateway records the action as failed.
Every send is recorded in comms.messages (delivery status arrives later by webhook) and comms.wa_routes, so a
reply can be routed back to the right tenant and customer. Phone numbers are decrypted only for the API call; the
routing table keeps a peppered hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from nirantar.channels.whatsapp import WhatsAppCloud, WhatsAppError
from nirantar.comms.sink import ChannelNotConnected, OutboundTemplate
from nirantar.core import crypto
from nirantar.db.session import tenant_tx
from nirantar.security.keys import hash_secret

SERVICE_WINDOW = timedelta(hours=24)


class TemplateNotApproved(ChannelNotConnected):
    pass


def wa_digits(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return "91" + digits if len(digits) == 10 else digits      # Indian numbers stored without country code


def phone_hash(digits: str) -> str:
    return hash_secret(f"wa:{digits}")


@dataclass
class ChannelSink:
    engine: Engine
    whatsapp: WhatsAppCloud | None = None
    speech: Any = None                     # SarvamSpeech for voice replies
    sent: list[str] = field(default_factory=list)

    @property
    def channels(self) -> frozenset[str]:
        return frozenset({"whatsapp"}) if self.whatsapp is not None else frozenset()

    # ---------------------------------------------------------------- helpers
    def _recipient(self, tenant_id: str, customer_id: str) -> tuple[str, str]:
        with tenant_tx(tenant_id, self.engine) as c:
            enc = c.execute(text("SELECT phone_enc FROM billing.customers WHERE customer_id=:c"),
                            {"c": customer_id}).scalar_one_or_none()
        if not enc:
            raise ChannelNotConnected("customer has no phone number on file")
        digits = wa_digits(crypto.decrypt(bytes(enc), tenant_id))
        return digits, phone_hash(digits)

    def session_open(self, tenant_id: str, ph: str, at: datetime) -> bool:
        with self.engine.connect() as c:
            last = c.execute(text("SELECT last_inbound_at FROM comms.wa_routes WHERE phone_hash=:h AND tenant_id=:t"),
                             {"h": ph, "t": tenant_id}).scalar_one_or_none()
        return last is not None and last > at - SERVICE_WINDOW

    def _approved(self, key: str, language: str) -> Any:
        with self.engine.connect() as c:
            for lang in dict.fromkeys((language, "en")):
                row = c.execute(text("SELECT meta_name, meta_language, params FROM config.whatsapp_templates WHERE "
                                     "template_key=:k AND language=:l AND status='APPROVED'"),
                                {"k": key, "l": lang}).one_or_none()
                if row is not None:
                    return row
        return None

    def _record(self, tenant_id: str, customer_id: str, ph: str, mid: str, kind: str, body: str,
                template_ref: str | None, at: datetime) -> None:
        with tenant_tx(tenant_id, self.engine) as c:
            c.execute(text("INSERT INTO comms.messages (tenant_id, message_id, customer_id, channel, direction, kind, "
                           "template_ref, status, body_sha256, body_enc, created_at, updated_at) VALUES (:t, :m, :c, "
                           "'whatsapp', 'outbound', :k, :tr, 'sent', :h, :b, :n, :n) ON CONFLICT DO NOTHING"),
                      {"t": tenant_id, "m": mid, "c": customer_id, "k": kind, "tr": template_ref,
                       "h": hashlib.sha256(body.encode()).hexdigest(), "b": crypto.encrypt(body, tenant_id),
                       "n": at})
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO comms.wa_message_ids (message_id, tenant_id) VALUES (:m, :t) "
                           "ON CONFLICT DO NOTHING"), {"m": mid, "t": tenant_id})
            c.execute(text("INSERT INTO comms.wa_routes (phone_hash, tenant_id, customer_id, last_outbound_at) VALUES "
                           "(:h, :t, :c, :n) ON CONFLICT (phone_hash, tenant_id) DO UPDATE SET "
                           "customer_id=EXCLUDED.customer_id, last_outbound_at=EXCLUDED.last_outbound_at"),
                      {"h": ph, "t": tenant_id, "c": customer_id, "n": at})
        self.sent.append(mid)

    # ---------------------------------------------------------------- CommsSink
    def send(self, channel: str, to_ref: str, text_: str, at: datetime, *, tenant_id: str | None = None,
             template: OutboundTemplate | None = None) -> str:
        if channel != "whatsapp":
            raise ChannelNotConnected(f"{channel}: on hold (SMS needs DLT registration)")
        if self.whatsapp is None:
            raise ChannelNotConnected("whatsapp: not configured for this deployment")
        if tenant_id is None:
            raise ValueError("tenant_id is required to send")
        digits, ph = self._recipient(tenant_id, to_ref)
        try:
            if self.session_open(tenant_id, ph, at):
                mid, kind, ref = self.whatsapp.send_text(digits, text_), "text", None
            elif template is not None:
                row = self._approved(template.key, template.language)
                if row is None:
                    raise TemplateNotApproved(f"no Meta-approved WhatsApp template for {template.key}; outside the "
                                              "24-hour window WhatsApp only delivers approved templates")
                params = [str(template.values.get(p, "")) for p in row.params]
                mid = self.whatsapp.send_template(digits, row.meta_name, row.meta_language, params)
                kind, ref = "template", f"{row.meta_name}@{row.meta_language}"
            else:
                raise TemplateNotApproved("the customer has not written in the last 24 hours and this message has "
                                          "no template")
        except WhatsAppError as exc:
            raise ChannelNotConnected(str(exc)) from exc
        self._record(tenant_id, to_ref, ph, mid, kind, text_, ref, at)
        return mid

    def send_voice(self, tenant_id: str, customer_id: str, text_: str, language: str, at: datetime) -> str:
        """A spoken reply (Sarvam TTS) as a WhatsApp audio message. Only inside the service window."""
        if self.whatsapp is None or self.speech is None:
            raise ChannelNotConnected("voice replies need WhatsApp and Sarvam configured")
        digits, ph = self._recipient(tenant_id, customer_id)
        if not self.session_open(tenant_id, ph, at):
            raise TemplateNotApproved("voice replies are only sent inside the 24-hour service window")
        audio = self.speech.tts_file(text_, language, codec="mp3")
        try:
            media = self.whatsapp.upload_media(audio, "audio/mpeg", "reply.mp3")
            mid = self.whatsapp.send_audio(digits, media)
        except WhatsAppError as exc:
            raise ChannelNotConnected(str(exc)) from exc
        self._record(tenant_id, customer_id, ph, mid, "audio", text_, None, at)
        return mid

    def accepted(self, provider_message_id: str) -> bool:
        with self.engine.connect() as c:
            tenant = c.execute(text("SELECT tenant_id FROM comms.wa_message_ids WHERE message_id=:m"),
                               {"m": provider_message_id}).scalar_one_or_none()
        if tenant is None:
            return False
        with tenant_tx(tenant, self.engine) as c:
            st = c.execute(text("SELECT status FROM comms.messages WHERE message_id=:m"),
                           {"m": provider_message_id}).scalar_one_or_none()
        return st in ("sent", "delivered", "read")
