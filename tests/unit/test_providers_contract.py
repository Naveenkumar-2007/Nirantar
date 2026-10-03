"""Provider contract tests built from documented payload shapes (docs/integrations/*.md)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest

from nirantar.core.money import Money
from nirantar.payments.domain import (
    Capability,
    LinkRequest,
    NotSupported,
    PaymentStatus,
    ProviderAuthError,
    ProviderRejected,
    ProviderUnavailable,
    SubscriptionStatus,
    require,
)
from nirantar.payments.http import CircuitBreaker, ProviderHttp
from nirantar.payments.providers.cashfree import CashfreeProvider
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.providers.razorpay import RazorpayProvider, _error_parser

PAYMENT = {
    "id": "pay_29QQoUBi66xm2f", "entity": "payment", "amount": 99900, "currency": "INR", "status": "failed",
    "method": "upi", "error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_balance",
    "created_at": 1759000000, "customer_id": "cust_1", "order_id": "order_1", "notes": {},
}


def razorpay_with(handler: Any, attempts: int = 3) -> tuple[RazorpayProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[no-any-return]

    http = ProviderHttp(
        provider="razorpay",
        client=httpx.Client(transport=httpx.MockTransport(_h), auth=("k", "s")),
        max_attempts=attempts, sleep=lambda _s: None, error_parser=_error_parser,
    )
    return RazorpayProvider("k", "s", http=http), seen


def test_razorpay_payment_mapping() -> None:
    p = RazorpayProvider.to_payment(PAYMENT)
    assert p.amount == Money.of("999") and p.status == PaymentStatus.FAILED
    assert p.error_reason == "insufficient_balance"
    refunded = RazorpayProvider.to_payment({**PAYMENT, "status": "captured", "amount_refunded": 50000})
    assert refunded.status == PaymentStatus.PARTIALLY_REFUNDED


def test_razorpay_get_retries_on_503_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={}) if calls["n"] < 3 else httpx.Response(200, json=PAYMENT)

    rzp, _ = razorpay_with(handler)
    assert rzp.fetch_payment("pay_29QQoUBi66xm2f").provider_payment_id == "pay_29QQoUBi66xm2f"
    assert calls["n"] == 3


def test_razorpay_business_rejection_is_not_retried() -> None:
    rzp, seen = razorpay_with(
        lambda _r: httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "bad"}})
    )
    with pytest.raises(ProviderRejected) as exc:
        rzp.fetch_payment("x")
    assert exc.value.code == "BAD_REQUEST_ERROR" and len(seen) == 1


def test_auth_errors_surface_distinctly() -> None:
    rzp, _ = razorpay_with(lambda _r: httpx.Response(401, json={"error": {"code": "AUTH", "description": "no"}}))
    with pytest.raises(ProviderAuthError):
        rzp.fetch_payment("x")


def test_payment_link_timeout_does_not_create_duplicates() -> None:
    """POST times out after the provider created the link; we must find it by reference_id, not re-POST."""
    posts = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "POST":
            posts["n"] += 1
            raise httpx.ReadTimeout("slow", request=r)
        return httpx.Response(200, json={"payment_links": [
            {"id": "plink_1", "short_url": "https://rzp.io/i/x", "amount": 99900, "status": "created",
             "reference_id": "ref_123"}]})

    rzp, _ = razorpay_with(handler)
    link = rzp.create_payment_link(LinkRequest(Money.of("999"), "ref_123", "Sub renewal", None, None, None))
    assert link.link_id == "plink_1" and posts["n"] == 1


def test_circuit_breaker_opens_and_half_opens() -> None:
    now = {"t": 0.0}
    breaker = CircuitBreaker(failure_threshold=2, reset_after_s=10, clock=lambda: now["t"])
    http = ProviderHttp("razorpay", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
                        max_attempts=1, breaker=breaker, sleep=lambda _s: None)
    rzp = RazorpayProvider("k", "s", http=http)
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            rzp.fetch_payment("x")
    with pytest.raises(ProviderUnavailable, match="circuit open"):
        rzp.fetch_payment("x")
    now["t"] = 11
    with pytest.raises(ProviderUnavailable) as exc:  # half-open probe goes through (and fails again)
        rzp.fetch_payment("x")
    assert "circuit open" not in str(exc.value)


def test_razorpay_webhook_signature_and_normalisation() -> None:
    body = json.dumps({"entity": "event", "event": "payment.failed", "created_at": 1759000000,
                       "payload": {"payment": {"entity": PAYMENT}}}).encode()
    secret = "whsec_test"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    rzp = RazorpayProvider("k", "s")
    headers = {"X-Razorpay-Signature": sig, "x-razorpay-event-id": "evt_1"}
    assert rzp.verify_webhook(headers, body, secret)
    assert not rzp.verify_webhook(headers, body + b" ", secret)       # body tampered
    assert not rzp.verify_webhook(headers, body, "wrong-secret")
    assert not rzp.verify_webhook({}, body, secret)
    evt = rzp.parse_webhook(headers, body)
    assert (evt.event_type, evt.provider_event_id, evt.subject_ref) == ("payment.failed", "evt_1", PAYMENT["id"])


def test_razorpay_upi_dummy_payment_is_registration_success() -> None:
    body = json.dumps({"event": "payment.failed", "payload": {"payment": {"entity": {
        **PAYMENT, "error_reason": "upi_dummy_payment"}}}}).encode()
    assert RazorpayProvider("k", "s").parse_webhook({}, body).event_type == "mandate.registration_succeeded"


def test_cashfree_webhook_signature_with_replay_window() -> None:
    cf = CashfreeProvider("id", "secret")
    body = b'{"type":"SUBSCRIPTION_PAYMENT_FAILED","data":{"subscription_details":{"subscription_id":"s1"}}}'
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b"secret", ts.encode() + body, hashlib.sha256).digest()).decode()
    headers = {"x-webhook-timestamp": ts, "x-webhook-signature": sig}
    assert cf.verify_webhook(headers, body, "secret")
    assert not cf.verify_webhook(headers, body, "secret", now=time.time() + 3600)  # replayed an hour later
    assert not cf.verify_webhook({**headers, "x-webhook-timestamp": str(int(ts) + 1)}, body, "secret")
    evt = cf.parse_webhook(headers, body)
    assert (evt.event_type, evt.subject_ref) == ("subscription.charge_failed", "s1")


def test_cashfree_mappings_and_capabilities() -> None:
    p = CashfreeProvider.to_payment("order_9", {"cf_payment_id": 123, "payment_status": "SUCCESS",
                                                "payment_amount": 999.0, "payment_currency": "INR"})
    assert p.provider_payment_id == "order_9/123" and p.amount == Money.of("999")
    s = CashfreeProvider.to_subscription({"subscription_id": "s1", "subscription_status": "ON_HOLD"})
    assert s.status == SubscriptionStatus.HALTED
    with pytest.raises(NotSupported):
        list(CashfreeProvider("id", "secret").list_payments(None, None))  # type: ignore[arg-type]
    require(CashfreeProvider("id", "secret"), Capability.REQUEST_IDEMPOTENCY)
    with pytest.raises(NotSupported):
        require(RazorpayProvider("k", "s"), Capability.REQUEST_IDEMPOTENCY)


def test_mock_provider_is_idempotent_and_emits_verifiable_webhooks() -> None:
    mock = MockProvider()
    sub = mock.add_subscription("cust_1", Money.of("999"))
    failed = mock.charge(sub, succeed=False)
    headers, body = mock.webhook_for("payment.failed", "payment", MockProvider.payment_entity(failed))
    assert mock.verify_webhook(headers, body, mock.webhook_secret)
    assert mock.parse_webhook(headers, body).event_type == "payment.failed"
    req = LinkRequest(Money.of("999"), "ref_1", "renewal", None, None, None)
    assert mock.create_payment_link(req) == mock.create_payment_link(req)
    paid = mock.pay_link(mock.create_payment_link(req).link_id)
    r1 = mock.create_refund(paid.provider_payment_id, Money.of("999"), "rf-1")
    assert mock.create_refund(paid.provider_payment_id, Money.of("999"), "rf-1") == r1
    mock.outage = True
    with pytest.raises(ProviderUnavailable):
        mock.fetch_payment(paid.provider_payment_id)
