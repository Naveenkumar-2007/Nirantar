"""Cashfree adapter (x-api-version 2026-01-01). Facts in docs/integrations/cashfree.md.

No credentials yet: contract-tested with fixtures built from the official docs.
Paths marked UNVERIFIED are exercised by the sandbox test suite once keys exist.

Cashfree identifies PG payments by (order_id, cf_payment_id). Nirantar encodes
that pair as provider_payment_id = "<order_id>/<cf_payment_id>".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from nirantar.core.money import Money
from nirantar.payments.domain import (
    Capability,
    LinkRequest,
    NormalizedWebhook,
    NotSupported,
    PaymentLink,
    PaymentStatus,
    ProviderError,
    ProviderPayment,
    ProviderSubscription,
    Refund,
    SubscriptionStatus,
    lower_headers,
)
from nirantar.payments.http import ProviderHttp

SANDBOX_URL = "https://sandbox.cashfree.com/pg"
PRODUCTION_URL = "https://api.cashfree.com/pg"
API_VERSION = "2026-01-01"
WEBHOOK_TOLERANCE_S = 300  # Cashfree specifies none; Nirantar enforces 5 minutes (replay protection)

_PAYMENT_STATUS = {
    "SUCCESS": PaymentStatus.CAPTURED,
    "FAILED": PaymentStatus.FAILED,
    "PENDING": PaymentStatus.CREATED,
    "NOT_ATTEMPTED": PaymentStatus.CREATED,
    "USER_DROPPED": PaymentStatus.FAILED,
    "CANCELLED": PaymentStatus.FAILED,
}
_SUB_STATUS = {
    "INITIALIZED": SubscriptionStatus.CREATED,
    "BANK_APPROVAL_PENDING": SubscriptionStatus.CREATED,
    "ACTIVE": SubscriptionStatus.ACTIVE,
    "ON_HOLD": SubscriptionStatus.HALTED,
    "CUSTOMER_PAUSED": SubscriptionStatus.PAUSED,
    "PAUSED": SubscriptionStatus.PAUSED,
    "CUSTOMER_CANCELLED": SubscriptionStatus.CANCELLED,
    "CANCELLED": SubscriptionStatus.CANCELLED,
    "EXPIRED": SubscriptionStatus.CANCELLED,
    "LINK_EXPIRED": SubscriptionStatus.CANCELLED,
    "CARD_EXPIRED": SubscriptionStatus.HALTED,
    "COMPLETED": SubscriptionStatus.COMPLETED,
}
EVENT_MAP = {
    "PAYMENT_SUCCESS_WEBHOOK": "payment.captured",        # name UNVERIFIED
    "PAYMENT_FAILED_WEBHOOK": "payment.failed",           # name UNVERIFIED
    "PAYMENT_USER_DROPPED_WEBHOOK": "payment.failed",     # name UNVERIFIED
    "SUBSCRIPTION_PAYMENT_SUCCESS": "subscription.charged",
    "SUBSCRIPTION_PAYMENT_FAILED": "subscription.charge_failed",
    "SUBSCRIPTION_PAYMENT_CANCELLED": "subscription.charge_cancelled",
    "SUBSCRIPTION_PAYMENT_NOTIFICATION_INITIATED": "mandate.predebit_notified",
    "SUBSCRIPTION_STATUS_CHANGED": "subscription.updated",
    "SUBSCRIPTION_AUTH_STATUS": "mandate.auth_status",
    "SUBSCRIPTION_CARD_EXPIRY_REMINDER": "mandate.card_expiring",
    "SUBSCRIPTION_REFUND_STATUS": "payment.refund_updated",
    "PAYMENT_LINK_EVENT": "payment.link_updated",
    "DISPUTE_CREATED": "dispute.created",
    "DISPUTE_UPDATED": "dispute.updated",
    "DISPUTE_CLOSED": "dispute.closed",
}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _error_parser(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        body = resp.json()
        return body.get("code"), f"{body.get('code')}: {body.get('message')}"
    except ValueError:
        return None, resp.text[:300]


class CashfreeProvider:
    name = "cashfree"
    capabilities = frozenset(
        {
            Capability.SUBSCRIPTIONS,
            Capability.PAUSE_SUBSCRIPTION,
            Capability.CHARGE_SUBSCRIPTION,
            Capability.PAYMENT_LINKS,
            Capability.REFUNDS,
            Capability.REFUND_IDEMPOTENCY,     # merchant-supplied refund_id
            Capability.REQUEST_IDEMPOTENCY,    # x-idempotency-key (verified on create subscription)
            Capability.DISPUTES,
            Capability.WEBHOOK_TIMESTAMP,
        }
    )

    def __init__(self, client_id: str, client_secret: str, *, sandbox: bool = True,
                 http: ProviderHttp | None = None, timeout_s: float = 10.0) -> None:
        if not client_id or not client_secret:
            raise ValueError("cashfree credentials required")
        self._base = SANDBOX_URL if sandbox else PRODUCTION_URL
        self._http = http or ProviderHttp(
            provider=self.name,
            client=httpx.Client(
                timeout=timeout_s,
                headers={"x-client-id": client_id, "x-client-secret": client_secret, "x-api-version": API_VERSION},
            ),
            error_parser=_error_parser,
        )

    @staticmethod
    def split_payment_id(provider_payment_id: str) -> tuple[str, str]:
        order_id, _, cf_payment_id = provider_payment_id.partition("/")
        if not order_id or not cf_payment_id:
            raise ProviderError("cashfree payment id must be '<order_id>/<cf_payment_id>'")
        return order_id, cf_payment_id

    @staticmethod
    def to_payment(order_id: str, p: Mapping[str, Any]) -> ProviderPayment:
        err = p.get("error_details") or {}
        amount = Money.of(str(p.get("payment_amount", "0")), p.get("payment_currency", "INR"))
        return ProviderPayment(
            provider="cashfree",
            provider_payment_id=f"{order_id}/{p.get('cf_payment_id')}",
            amount=amount,
            status=_PAYMENT_STATUS.get(str(p.get("payment_status", "")).upper(), PaymentStatus.CREATED),
            method=p.get("payment_group"),
            error_code=err.get("error_code"),
            error_reason=err.get("error_reason") or p.get("payment_message"),
            created_at=_parse_time(p.get("payment_time")),
            order_ref=order_id,
        )

    @staticmethod
    def to_subscription(s: Mapping[str, Any]) -> ProviderSubscription:
        return ProviderSubscription(
            provider="cashfree",
            provider_subscription_id=str(s.get("subscription_id")),
            status=_SUB_STATUS.get(str(s.get("subscription_status", "")).upper(), SubscriptionStatus.CREATED),
            plan_ref=(s.get("plan_details") or {}).get("plan_id"),
            customer_ref=(s.get("customer_details") or {}).get("customer_email"),
            current_end=_parse_time(s.get("subscription_expiry_time")),
            charge_at=_parse_time(s.get("next_schedule_date")),
            paid_count=0,
            remaining_count=None,
        )

    def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        order_id, cf_id = self.split_payment_id(provider_payment_id)
        return self.to_payment(order_id, self._http.request(
            "GET", f"{self._base}/orders/{order_id}/payments/{cf_id}", idempotent=True))

    def list_payments(self, since: datetime, until: datetime) -> Iterator[ProviderPayment]:
        raise NotSupported("cashfree has no account-wide payment listing; reconcile per order or subscription")

    def create_payment_link(self, request: LinkRequest) -> PaymentLink:
        body: dict[str, Any] = {
            "link_id": request.reference_id,  # merchant-unique: natural idempotency
            "link_amount": float(request.amount.to_decimal()),
            "link_currency": request.amount.currency,
            "link_purpose": request.description[:500],
            "customer_details": {k: v for k, v in {
                "customer_phone": request.customer_phone, "customer_email": request.customer_email,
                "customer_name": request.customer_name}.items() if v},
            "link_notify": {"send_sms": request.notify_sms, "send_email": request.notify_email},
        }
        if request.expire_by is not None:
            body["link_expiry_time"] = request.expire_by.isoformat()
        link = self._http.request("POST", f"{self._base}/links", idempotent=True, json=body,
                                  headers={"x-idempotency-key": request.reference_id})
        return PaymentLink("cashfree", link["link_id"], link["link_url"], request.amount,
                           link.get("link_status", "ACTIVE"), request.reference_id)

    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self.to_subscription(self._http.request(
            "GET", f"{self._base}/subscriptions/{provider_subscription_id}", idempotent=True))

    def list_subscription_payments(self, provider_subscription_id: str) -> list[ProviderPayment]:
        items = self._http.request("GET", f"{self._base}/subscriptions/{provider_subscription_id}/payments",
                                   idempotent=True)
        rows = items if isinstance(items, list) else items.get("payments", [])
        return [
            ProviderPayment(
                provider="cashfree",
                provider_payment_id=f"{provider_subscription_id}/{p.get('payment_id')}",
                amount=Money.of(str(p.get("payment_amount", "0"))),
                status=_PAYMENT_STATUS.get(str(p.get("payment_status", "")).upper(), PaymentStatus.CREATED),
                method=p.get("payment_type"),
                error_code=(p.get("failure_details") or {}).get("failure_reason"),
                error_reason=(p.get("failure_details") or {}).get("failure_reason"),
                created_at=_parse_time(p.get("payment_initiated_date")),
                subscription_ref=provider_subscription_id,
            )
            for p in rows
        ]

    def _manage(self, provider_subscription_id: str, action: str) -> ProviderSubscription:
        # PUT /subscriptions/{id} "Manage" endpoint (verified); body shape {"action": ...} UNVERIFIED.
        return self.to_subscription(self._http.request(
            "PUT", f"{self._base}/subscriptions/{provider_subscription_id}", idempotent=False,
            json={"subscription_id": provider_subscription_id, "action": action}))

    def pause_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self._manage(provider_subscription_id, "PAUSE")

    def resume_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        return self._manage(provider_subscription_id, "ACTIVATE")

    def create_refund(self, provider_payment_id: str, amount: Money, idempotency_key: str) -> Refund:
        order_id, _ = self.split_payment_id(provider_payment_id)
        r = self._http.request(
            "POST", f"{self._base}/orders/{order_id}/refunds", idempotent=True,
            json={"refund_amount": float(amount.to_decimal()), "refund_id": idempotency_key[:40],
                  "refund_note": "nirantar"},
        )
        return Refund("cashfree", str(r.get("refund_id", idempotency_key)), provider_payment_id, amount,
                      str(r.get("refund_status", "PENDING")))

    # ---- webhooks -------------------------------------------------------------
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str,
                       now: float | None = None) -> bool:
        h = lower_headers(headers)
        timestamp, signature = h.get("x-webhook-timestamp", ""), h.get("x-webhook-signature", "")
        if not timestamp or not signature or not secret:
            return False
        try:
            ts = int(timestamp)
        except ValueError:
            return False
        ts_s = ts / 1000 if ts > 10**11 else ts  # accept ms or s (unit UNVERIFIED)
        if abs((now if now is not None else time.time()) - ts_s) > WEBHOOK_TOLERANCE_S:
            return False
        digest = hmac.new(secret.encode(), timestamp.encode() + raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    def parse_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> NormalizedWebhook:
        h = lower_headers(headers)
        body = json.loads(raw_body)
        provider_type = str(body.get("type", ""))
        event_type = EVENT_MAP.get(provider_type)
        if event_type is None:
            raise ProviderError(f"unmapped cashfree event {provider_type!r}", code="unmapped_event")
        data = body.get("data", {})
        subject = str(
            (data.get("subscription_details") or {}).get("subscription_id")
            or (data.get("payment") or {}).get("cf_payment_id")
            or (data.get("order") or {}).get("order_id")
            or (data.get("dispute") or {}).get("dispute_id")
            or "unknown"
        )
        event_id = h.get("x-idempotency-header") or hashlib.sha256(raw_body).hexdigest()
        return NormalizedWebhook("cashfree", event_id, provider_type, event_type,
                                 _parse_time(body.get("event_time")), subject, data)
