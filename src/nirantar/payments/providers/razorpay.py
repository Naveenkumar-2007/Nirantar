"""Razorpay adapter. Facts verified in docs/integrations/razorpay.md (2026-09-28).

Idempotency (ADR-0003): Razorpay has no general idempotency header.
- Refunds: `receipt` dedupes per payment -> safe to retry.
- Payment links: `reference_id` must be unique -> on an ambiguous failure we look
  the link up by reference_id before retrying, so a timeout never creates two links.
- Everything else: Nirantar-side idempotency (core.idempotency_keys).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from nirantar.core.money import Money
from nirantar.payments.domain import (
    Capability,
    LinkRequest,
    NormalizedWebhook,
    PaymentLink,
    PaymentStatus,
    ProviderDispute,
    ProviderError,
    ProviderMandate,
    ProviderPayment,
    ProviderRejected,
    ProviderSubscription,
    ProviderUnavailable,
    Refund,
    RegistrationLink,
    SubscriptionStatus,
    lower_headers,
)
from nirantar.payments.http import ProviderHttp

BASE_URL = "https://api.razorpay.com/v1"

EVENT_MAP: dict[str, str] = {
    "payment.authorized": "payment.authorized",
    "payment.captured": "payment.captured",
    "payment.failed": "payment.failed",
    "order.paid": "payment.order_paid",
    "payment.downtime.started": "bank.downtime_started",
    "payment.downtime.updated": "bank.downtime_updated",
    "payment.downtime.resolved": "bank.downtime_resolved",
    "subscription.authenticated": "subscription.authenticated",
    "subscription.activated": "subscription.activated",
    "subscription.charged": "subscription.charged",
    "subscription.pending": "subscription.pending",
    "subscription.halted": "subscription.halted",
    "subscription.cancelled": "subscription.cancelled",
    "subscription.completed": "subscription.completed",
    "subscription.updated": "subscription.updated",
    "subscription.paused": "subscription.paused",
    "subscription.resumed": "subscription.resumed",
    "refund.created": "payment.refund_created",
    "refund.processed": "payment.refund_processed",
    "refund.failed": "payment.refund_failed",
    "payment.dispute.created": "dispute.created",
    "payment.dispute.won": "dispute.won",
    "payment.dispute.lost": "dispute.lost",
    "payment.dispute.closed": "dispute.closed",
    "payment.dispute.under_review": "dispute.under_review",
    "payment.dispute.action_required": "dispute.action_required",
    "token.confirmed": "mandate.confirmed",
    "token.rejected": "mandate.rejected",
    "token.cancelled": "mandate.cancelled",
    "token.paused": "mandate.paused",
    "payment_link.paid": "payment.link_paid",
}

_PAYMENT_STATUS = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "refunded": PaymentStatus.REFUNDED,
    "failed": PaymentStatus.FAILED,
}
_SUB_STATUS = {
    "created": SubscriptionStatus.CREATED,
    "authenticated": SubscriptionStatus.CREATED,
    "active": SubscriptionStatus.ACTIVE,
    "pending": SubscriptionStatus.ACTIVE,  # charge failed, provider retrying
    "halted": SubscriptionStatus.HALTED,
    "paused": SubscriptionStatus.PAUSED,
    "cancelled": SubscriptionStatus.CANCELLED,
    "expired": SubscriptionStatus.CANCELLED,
    "completed": SubscriptionStatus.COMPLETED,
}


# Razorpay token recurring_details.status → Nirantar mandate status (docs 2026-10-01)
_TOKEN_STATUS = {"initiated": "pending", "confirmed": "active", "rejected": "failed", "cancelled": "revoked",
                 "paused": "paused"}
_RAIL = {"upi": "upi_autopay", "emandate": "emandate", "card": "card", "nach": "nach"}
# Razorpay dispute status → Nirantar (docs 2026-10-01: open → under_review after contest; won / lost / closed)
_DISPUTE_STATUS = {"open": "open", "under_review": "submitted", "won": "won", "lost": "lost", "closed": "accepted"}


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _error_parser(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        body = resp.json()
    except ValueError:
        return None, resp.text[:300]
    err = body.get("error", {}) if isinstance(body, dict) else {}
    if isinstance(err, str):                  # some endpoints answer {"error": "<message>"}
        return None, err[:300]
    if not isinstance(err, dict):
        return None, str(body)[:300]
    return err.get("code"), f"{err.get('code')}: {err.get('description')}"


class RazorpayProvider:
    name = "razorpay"
    capabilities = frozenset(
        {
            Capability.SUBSCRIPTIONS,
            Capability.PAUSE_SUBSCRIPTION,
            Capability.CHARGE_SUBSCRIPTION,
            Capability.PAYMENT_LINKS,
            Capability.REFUNDS,
            Capability.REFUND_IDEMPOTENCY,
            Capability.DISPUTES,
        }
    )

    def __init__(self, key_id: str, key_secret: str, *, http: ProviderHttp | None = None,
                 base_url: str = BASE_URL, timeout_s: float = 10.0) -> None:
        if not key_id or not key_secret:
            raise ValueError("razorpay credentials required")
        self._base = base_url.rstrip("/")
        self._http = http or ProviderHttp(
            provider=self.name,
            client=httpx.Client(auth=(key_id, key_secret), timeout=timeout_s),
            error_parser=_error_parser,
        )

    # ---- mapping ------------------------------------------------------------
    @staticmethod
    def to_payment(p: Mapping[str, Any]) -> ProviderPayment:
        raw_notes = p.get("notes")
        notes: dict[str, str] = (
            {str(k): str(v) for k, v in raw_notes.items()} if isinstance(raw_notes, dict) else {}
        )
        return ProviderPayment(
            provider="razorpay",
            provider_payment_id=p["id"],
            amount=Money(int(p["amount"]), p.get("currency", "INR")),
            status=_PAYMENT_STATUS.get(p.get("status", ""), PaymentStatus.CREATED)
            if not (p.get("status") == "captured" and p.get("amount_refunded"))
            else (
                PaymentStatus.REFUNDED
                if int(p.get("amount_refunded", 0)) >= int(p["amount"])
                else PaymentStatus.PARTIALLY_REFUNDED
            ),
            method=p.get("method"),
            error_code=p.get("error_code"),
            error_reason=p.get("error_reason") or p.get("error_description"),
            created_at=_ts(p.get("created_at")),
            customer_ref=p.get("customer_id"),
            subscription_ref=notes.get("subscription_id") or p.get("subscription_id"),
            order_ref=p.get("order_id"),
            notes=notes,
            token_ref=p.get("token_id"),
        )

    @staticmethod
    def to_subscription(s: Mapping[str, Any]) -> ProviderSubscription:
        return ProviderSubscription(
            provider="razorpay",
            provider_subscription_id=s["id"],
            status=_SUB_STATUS.get(s.get("status", ""), SubscriptionStatus.CREATED),
            plan_ref=s.get("plan_id"),
            customer_ref=s.get("customer_id"),
            current_end=_ts(s.get("current_end")),
            charge_at=_ts(s.get("charge_at")),
            paid_count=int(s.get("paid_count") or 0),
            remaining_count=int(s["remaining_count"]) if s.get("remaining_count") is not None else None,
        )

    # ---- API ----------------------------------------------------------------
    def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        return self.to_payment(self._http.request("GET", f"{self._base}/payments/{provider_payment_id}",
                                                  idempotent=True))

    def list_payments(self, since: datetime, until: datetime) -> Iterator[ProviderPayment]:
        skip = 0
        while True:
            page = self._http.request(
                "GET", f"{self._base}/payments", idempotent=True,
                params={"from": int(since.timestamp()), "to": int(until.timestamp()), "count": 100, "skip": skip},
            )
            items = page.get("items", [])
            for item in items:
                yield self.to_payment(item)
            if len(items) < 100:
                return
            skip += 100

    def _find_link(self, reference_id: str) -> Mapping[str, Any] | None:
        # Filtering by reference_id on the list endpoint is UNVERIFIED; we filter client-side as a fallback.
        page = self._http.request("GET", f"{self._base}/payment_links", idempotent=True,
                                  params={"reference_id": reference_id})
        for link in page.get("payment_links", page.get("items", [])):
            if link.get("reference_id") == reference_id:
                return dict(link)
        return None

    def create_payment_link(self, request: LinkRequest) -> PaymentLink:
        if request.amount.currency != "INR":
            raise ProviderRejected("razorpay payment links are created in INR in Nirantar")
        body: dict[str, Any] = {
            "amount": request.amount.minor,
            "currency": "INR",
            "accept_partial": False,
            "reference_id": request.reference_id,
            "description": request.description[:2048],
            "notify": {"sms": request.notify_sms, "email": request.notify_email},
            "reminder_enable": False,  # reminders are Nirantar's job (contact budget + compliance)
            "notes": {"nirantar_ref": request.reference_id},
        }
        customer = {k: v for k, v in {"name": request.customer_name, "contact": request.customer_phone,
                                      "email": request.customer_email}.items() if v}
        if customer:
            body["customer"] = customer
        if request.expire_by is not None:
            body["expire_by"] = int(request.expire_by.timestamp())
        try:
            link = self._http.request("POST", f"{self._base}/payment_links", idempotent=False, json=body)
        except ProviderUnavailable:
            existing = self._find_link(request.reference_id)  # did the ambiguous POST succeed?
            if existing is None:
                raise
            link = existing
        except ProviderRejected as exc:
            existing = self._find_link(request.reference_id) if "reference" in str(exc).lower() else None
            if existing is None:
                raise
            link = existing
        return PaymentLink("razorpay", link["id"], link["short_url"], Money(int(link["amount"])),
                           link.get("status", "created"), request.reference_id)

    @staticmethod
    def to_mandate(t: Mapping[str, Any], customer_ref: str | None = None) -> ProviderMandate:
        rd = t.get("recurring_details") or {}
        status = _TOKEN_STATUS.get(rd.get("status", ""), "pending")
        return ProviderMandate("razorpay", t["id"], customer_ref or t.get("customer_id"),
                               _RAIL.get(str(t.get("method")), "other"), status,
                               Money(int(t["max_amount"])) if t.get("max_amount") else None, _ts(t.get("expired_at")),
                               rd.get("failure_reason"))

    def mandate_from_webhook(self, entity: Mapping[str, Any]) -> ProviderMandate:
        return self.to_mandate(entity)

    def list_mandates(self, customer_ref: str) -> list[ProviderMandate]:
        """GET /v1/customers/{id}/tokens. Razorpay omits expired, rejected and unused tokens from this list."""
        body = self._http.request("GET", f"{self._base}/customers/{customer_ref}/tokens", idempotent=True)
        return [self.to_mandate(t, customer_ref) for t in body.get("items", []) if t.get("recurring")]

    def create_registration_link(self, *, customer_ref: str | None, name: str, contact: str | None,
                                 email: str | None, rail: str, max_amount: Money, expire_at: datetime, receipt: str,
                                 description: str) -> RegistrationLink:
        """POST /v1/subscription_registration/auth_links. The customer completes the authorisation themselves.
        Authorisation amount: 0 for e-mandate / NACH; ₹1 for UPI and cards (engineering assumption from Razorpay's
        examples; the API rejects an invalid amount, it is never charged as a subscription payment)."""
        method = {"upi_autopay": "upi", "emandate": "emandate", "card": "card", "nach": "nach"}.get(rail, "upi")
        # documented body: the customer object (name/contact/email); customer_ref is not part of this API
        body = {"customer": {k: v for k, v in {"name": name, "contact": contact, "email": email}.items() if v},
                "type": "link", "amount": 0 if method in ("emandate", "nach") else 100, "currency": max_amount.currency,
                "description": description[:250], "receipt": receipt[:40], "sms_notify": False, "email_notify": False,
                "subscription_registration": {"method": method, "max_amount": max_amount.minor,
                                              "expire_at": int(expire_at.timestamp())}}
        inv = self._http.request("POST", f"{self._base}/subscription_registration/auth_links", idempotent=False,
                                 json=body)
        return RegistrationLink("razorpay", inv["id"], inv["short_url"], inv.get("status", "issued"))

    @staticmethod
    def to_dispute(d: Mapping[str, Any]) -> ProviderDispute:
        return ProviderDispute("razorpay", d["id"], d["payment_id"], Money(int(d["amount"]), d.get("currency", "INR")),
                               str(d.get("reason_code") or "unknown"), _DISPUTE_STATUS.get(d.get("status", ""), "open"),
                               d.get("phase"), _ts(d.get("respond_by")), _ts(d.get("created_at")))

    def fetch_dispute(self, provider_dispute_id: str) -> ProviderDispute:
        """GET /v1/disputes/{id} — provider truth for a dispute webhook."""
        return self.to_dispute(self._http.request("GET", f"{self._base}/disputes/{provider_dispute_id}",
                                                  idempotent=True))

    def upload_document(self, filename: str, content: bytes, mime: str, purpose: str = "dispute_evidence") -> str:
        """POST /v1/documents (multipart: file, purpose). Returns the document id."""
        doc = self._http.request("POST", f"{self._base}/documents", idempotent=False,
                                 files={"file": (filename, content, mime)}, data={"purpose": purpose})
        return str(doc["id"])

    def contest_dispute(self, provider_dispute_id: str, *, summary: str, document_ids: Mapping[str, list[str]],
                        amount: Money | None = None, submit: bool = True) -> ProviderDispute:
        """PATCH /v1/disputes/{id}/contest with evidence document ids; action=submit (or draft)."""
        body: dict[str, Any] = {"summary": summary[:1000], "action": "submit" if submit else "draft",
                                **{k: list(v) for k, v in document_ids.items() if v}}
        if amount is not None:
            body["amount"] = amount.minor
        return self.to_dispute(self._http.request("PATCH", f"{self._base}/disputes/{provider_dispute_id}/contest",
                                                  idempotent=True, json=body))

    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self.to_subscription(
            self._http.request("GET", f"{self._base}/subscriptions/{provider_subscription_id}", idempotent=True)
        )

    def list_subscription_payments(self, provider_subscription_id: str) -> list[ProviderPayment]:
        # Invoices carry the payment_id for each subscription charge (path UNVERIFIED in docs; covered by
        # the live sandbox test when credentials allow).
        page = self._http.request("GET", f"{self._base}/invoices", idempotent=True,
                                  params={"subscription_id": provider_subscription_id, "count": 100})
        out = []
        for inv in page.get("items", []):
            if inv.get("payment_id"):
                out.append(self.fetch_payment(inv["payment_id"]))
        return out

    def pause_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self.to_subscription(self._http.request(
            "POST", f"{self._base}/subscriptions/{provider_subscription_id}/pause",
            idempotent=False, json={"pause_at": "now"}))

    def resume_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self.to_subscription(self._http.request(
            "POST", f"{self._base}/subscriptions/{provider_subscription_id}/resume",
            idempotent=False, json={"resume_at": "now"}))

    def create_refund(self, provider_payment_id: str, amount: Money, idempotency_key: str) -> Refund:
        if not amount.is_positive:
            raise ProviderRejected("refund amount must be positive")
        r = self._http.request(
            "POST", f"{self._base}/payments/{provider_payment_id}/refund", idempotent=True,  # receipt dedupes
            json={"amount": amount.minor, "speed": "normal", "receipt": idempotency_key[:40]},
        )
        return Refund("razorpay", r["id"], provider_payment_id, Money(int(r["amount"])), r.get("status", "pending"))

    # ---- webhooks -------------------------------------------------------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        return verify_razorpay_signature(headers, raw_body, secret)

    def parse_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> NormalizedWebhook:
        return parse_razorpay_webhook(headers, raw_body, provider="razorpay")


def verify_razorpay_signature(headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
    """HMAC-SHA256(secret, raw body) hex == X-Razorpay-Signature, constant-time compare."""
    signature = lower_headers(headers).get("x-razorpay-signature", "")
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_razorpay_webhook(headers: Mapping[str, str], raw_body: bytes, *, provider: str) -> NormalizedWebhook:
    h = lower_headers(headers)
    body = json.loads(raw_body)
    provider_type = str(body.get("event", ""))
    payload = body.get("payload", {})
    entities = {k: v.get("entity", v) for k, v in payload.items() if isinstance(v, dict)}
    event_type = EVENT_MAP.get(provider_type)
    if event_type is None:
        raise ProviderError(f"unmapped razorpay event {provider_type!r}", code="unmapped_event")
    payment = entities.get("payment", {})
    # A UPI/emandate registration can surface as a failed "dummy" payment: that is success.
    if provider_type == "payment.failed" and payment.get("error_reason") == "upi_dummy_payment":
        event_type = "mandate.registration_succeeded"
    subject = next(
        (entities[k]["id"] for k in ("subscription", "refund", "dispute", "token", "payment_link", "payment")
         if k in entities and isinstance(entities[k], dict) and "id" in entities[k]),
        "unknown",
    )
    event_id = h.get("x-razorpay-event-id") or hashlib.sha256(raw_body).hexdigest()
    return NormalizedWebhook(provider, event_id, provider_type, event_type, _ts(body.get("created_at")),
                             subject, entities)
