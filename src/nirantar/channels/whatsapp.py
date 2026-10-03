"""WhatsApp Cloud API client (Graph API v23.0; verified against the connected test number on 2026-10-02).

Messages:  POST /{phone_number_id}/messages            text | template | audio (by uploaded media id)
Media:     POST /{phone_number_id}/media (multipart)   → media id;  GET /{media_id} → url → GET url (bearer)
Templates: GET/POST /{waba_id}/message_templates       Meta reviews every new template before it can be sent
Webhooks:  GET  verification (hub.mode/hub.verify_token/hub.challenge)
           POST X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app_secret, raw body)

Business-initiated messages outside the customer's 24-hour service window must be approved templates (Meta error
131047 otherwise). Test numbers deliver only to the recipients verified in the Meta app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from nirantar.core.errors import NirantarError

GRAPH = "https://graph.facebook.com"
VERSION = "v23.0"
OUTSIDE_WINDOW = 131047          # re-engagement: more than 24 h since the customer's last message
RETRYABLE = {1, 2, 4, 80007, 130429, 131016, 131048, 131056}   # transient / throughput / rate-limit codes


class WhatsAppError(NirantarError):
    def __init__(self, code: int | None, title: str, details: str = "") -> None:
        super().__init__(f"whatsapp {code}: {title} {details}".strip())
        self.code, self.title, self.details = code, title, details

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


@dataclass
class WhatsAppCloud:
    phone_number_id: str
    access_token: str
    waba_id: str | None = None
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=20.0))

    @classmethod
    def from_env(cls) -> WhatsAppCloud | None:
        pid, tok = os.environ.get("WHATSAPP_PHONE_NUMBER_ID"), os.environ.get("WHATSAPP_ACCESS_TOKEN")
        if not pid or not tok:
            return None
        return cls(pid, tok, os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID"))

    # ---------------------------------------------------------------- http
    def _url(self, path: str) -> str:
        return f"{GRAPH}/{VERSION}/{path.lstrip('/')}"

    def _call(self, method: str, url: str, **kw: Any) -> Any:
        r = self.client.request(method, url, headers={"Authorization": f"Bearer {self.access_token}"}, **kw)
        if r.status_code >= 400:
            try:
                e = r.json().get("error", {})
            except ValueError:
                e = {}
            raise WhatsAppError(e.get("code"), e.get("message") or f"http {r.status_code}",
                                str((e.get("error_data") or {}).get("details") or ""))
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.content

    def _send(self, to: str, payload: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}
        return str(self._call("POST", self._url(f"{self.phone_number_id}/messages"), json=body)["messages"][0]["id"])

    # ---------------------------------------------------------------- send
    def send_text(self, to: str, body: str) -> str:
        return self._send(to, {"type": "text", "text": {"body": body[:4096], "preview_url": True}})

    def send_template(self, to: str, name: str, language: str, params: list[str]) -> str:
        tpl: dict[str, Any] = {"name": name, "language": {"code": language}}
        if params:
            tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._send(to, {"type": "template", "template": tpl})

    def send_audio(self, to: str, media_id: str) -> str:
        return self._send(to, {"type": "audio", "audio": {"id": media_id}})

    def upload_media(self, content: bytes, mime: str, filename: str = "reply.mp3") -> str:
        out = self._call("POST", self._url(f"{self.phone_number_id}/media"),
                         data={"messaging_product": "whatsapp", "type": mime},
                         files={"file": (filename, content, mime)})
        return str(out["id"])

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = self._call("GET", self._url(media_id))
        content = self._call("GET", meta["url"])
        return (content if isinstance(content, bytes) else json.dumps(content).encode()), str(meta.get("mime_type", ""))

    # ---------------------------------------------------------------- templates
    def list_templates(self) -> list[dict[str, Any]]:
        if not self.waba_id:
            raise WhatsAppError(None, "WHATSAPP_BUSINESS_ACCOUNT_ID not configured")
        out: list[dict[str, Any]] = []
        url: str | None = self._url(f"{self.waba_id}/message_templates")
        params: dict[str, Any] | None = {"fields": "name,language,status,category,id,rejected_reason", "limit": 100}
        while url:
            page = self._call("GET", url, params=params)
            out.extend(page.get("data", []))
            url, params = (page.get("paging") or {}).get("next"), None
        return out

    def create_template(self, name: str, language: str, category: str, body: str,
                        example: list[str]) -> dict[str, Any]:
        if not self.waba_id:
            raise WhatsAppError(None, "WHATSAPP_BUSINESS_ACCOUNT_ID not configured")
        comp: dict[str, Any] = {"type": "BODY", "text": body}
        if example:
            comp["example"] = {"body_text": [example]}
        out: dict[str, Any] = self._call("POST", self._url(f"{self.waba_id}/message_templates"), json={
            "name": name, "language": language, "category": category, "components": [comp]})
        return out


# ---------------------------------------------------------------- webhooks
def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    from_number: str               # E.164 digits without "+", as WhatsApp sends it
    kind: str                      # text | audio | image | document | button | interactive | other
    text: str | None
    media_id: str | None
    mime: str | None
    at: datetime
    profile_name: str | None = None


@dataclass(frozen=True)
class StatusUpdate:
    message_id: str
    status: str                    # sent | delivered | read | failed
    at: datetime
    error_code: str | None = None
    error_title: str | None = None


def parse_webhook(payload: dict[str, Any]) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind, text_, media, mime = m.get("type", "other"), None, None, None
                if kind == "text":
                    text_ = (m.get("text") or {}).get("body")
                elif kind == "button":
                    text_ = (m.get("button") or {}).get("text")
                elif kind == "interactive":
                    i = m.get("interactive") or {}
                    text_ = ((i.get("button_reply") or i.get("list_reply") or {}).get("title"))
                elif kind in ("audio", "image", "document", "video", "sticker"):
                    media, mime = (m.get(kind) or {}).get("id"), (m.get(kind) or {}).get("mime_type")
                    text_ = (m.get(kind) or {}).get("caption")
                messages.append(InboundMessage(m["id"], m["from"], kind, text_, media, mime,
                                               datetime.fromtimestamp(int(m.get("timestamp", 0)), UTC),
                                               names.get(m["from"])))
            for s in value.get("statuses", []):
                err = (s.get("errors") or [{}])[0]
                statuses.append(StatusUpdate(s["id"], s["status"],
                                             datetime.fromtimestamp(int(s.get("timestamp", 0)), UTC),
                                             str(err["code"]) if err.get("code") else None, err.get("title")))
    return messages, statuses
