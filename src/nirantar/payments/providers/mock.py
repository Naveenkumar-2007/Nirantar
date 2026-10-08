"""Deterministic in-memory provider for RecurSim, workflow tests and demos.

It speaks Razorpay's webhook format and signature scheme, so the real webhook
ingress path (verify → persist → normalise) is exercised end to end.
Failure injection lets tests drive outages, declines and timeouts.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from nirantar.core.money import Money
from nirantar.payments.domain import (
    Capability,
    CheckoutOrder,
    LinkRequest,
    LinkStatus,
    NormalizedWebhook,
    PaymentLink,
    PaymentStatus,
    ProviderDispute,
    ProviderMandate,
    ProviderPayment,
    ProviderRejected,
    ProviderSubscription,
    ProviderUnavailable,
    Refund,
    RegistrationLink,
    SubscriptionStatus,
)
from nirantar.payments.providers.razorpay import parse_razorpay_webhook, verify_razorpay_signature


@dataclass
class _Sub:
    sub_id: str
    customer_ref: str
    amount: Money
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    payments: list[str] = field(default_factory=list)
    token_ref: str | None = None


class MockProvider:
    name = "mock"
    capabilities = frozenset(
        {
            Capability.SUBSCRIPTIONS,
            Capability.PAUSE_SUBSCRIPTION,
            Capability.CHARGE_SUBSCRIPTION,
            Capability.PAYMENT_LINKS,
            Capability.REFUNDS,
            Capability.REFUND_IDEMPOTENCY,
            Capability.REQUEST_IDEMPOTENCY,
            Capability.WEBHOOK_REPLAY,
            Capability.CHECKOUT_ORDERS,
        }
    )

    def __init__(self, webhook_secret: str = "mock-secret") -> None:  # noqa: S107 - simulator-only secret
        self.webhook_secret = webhook_secret
        self._seq = itertools.count(1)
        self.payments: dict[str, ProviderPayment] = {}
        self.subs: dict[str, _Sub] = {}
        self.links: dict[str, PaymentLink] = {}
        self.link_payments: dict[str, list[str]] = {}
        self.orders: dict[str, CheckoutOrder] = {}
        # scripted outcomes for merchant-initiated mandate charges: (succeed, error_reason) popped in order;
        # empty → the charge succeeds
        self.mandate_outcomes: list[tuple[bool, str | None]] = []
        self.mandate_charges: list[dict[str, Any]] = []
        self.order_payments: dict[str, list[str]] = {}
        self.checkout_key = "rzp_test_mock"
        self.refunds: dict[str, Refund] = {}
        self.disputes: dict[str, ProviderDispute] = {}
        self.documents: dict[str, tuple[str, int]] = {}
        self.contests: list[dict[str, Any]] = []
        self.mandates: dict[str, ProviderMandate] = {}
        self.registrations: dict[str, dict[str, Any]] = {}
        self.outage = False               # every call raises ProviderUnavailable
        self.calls: list[tuple[str, Any]] = []

    def _next(self, prefix: str) -> str:
        return f"{prefix}_mock{next(self._seq):08d}"

    def _guard(self, op: str, arg: Any) -> None:
        self.calls.append((op, arg))
        if self.outage:
            raise ProviderUnavailable("mock: simulated outage")

    # ---- simulation controls -------------------------------------------------
    def add_subscription(self, customer_ref: str, amount: Money, token_ref: str | None = None) -> str:
        sub_id = self._next("sub")
        self.subs[sub_id] = _Sub(sub_id, customer_ref, amount, token_ref=token_ref)
        return sub_id

    def add_mandate(self, customer_ref: str, *, rail: str = "upi_autopay", max_amount: Money | None = None,
                    valid_until: datetime | None = None, status: str = "active") -> str:
        token = self._next("token")
        self.mandates[token] = ProviderMandate("mock", token, customer_ref, rail, status,
                                               max_amount or Money.of("5000"), valid_until)
        return token

    def set_mandate(self, token_id: str, status: str, failure_reason: str | None = None) -> ProviderMandate:
        m = self.mandates[token_id]
        new = ProviderMandate(m.provider, m.provider_token_id, m.customer_ref, m.rail, status, m.max_amount,
                              m.valid_until, failure_reason)
        self.mandates[token_id] = new
        return new

    @staticmethod
    def token_entity(m: ProviderMandate) -> dict[str, Any]:
        """Razorpay token entity shape (no customer id: Razorpay's token webhooks don't carry one)."""
        status = {"pending": "initiated", "active": "confirmed", "failed": "rejected", "revoked": "cancelled",
                  "paused": "paused", "expired": "confirmed"}[m.status]
        return {"id": m.provider_token_id, "entity": "token", "recurring": True, "method": "upi",
                "recurring_details": {"status": status, "failure_reason": m.failure_reason},
                "max_amount": m.max_amount.minor if m.max_amount else None,
                "expired_at": int(m.valid_until.timestamp()) if m.valid_until else None}

    def complete_registration(self, link_id: str) -> tuple[str, ProviderPayment]:
        """Simulator: the customer opens the link and authorises a new mandate. Like Razorpay, this records an
        authorisation payment carrying the new token."""
        reg = self.registrations[link_id]
        token = self.add_mandate(reg["customer_ref"], rail=reg["rail"], max_amount=reg["max_amount"])
        reg["status"] = "paid"
        for sub in self.subs.values():
            if sub.customer_ref == reg["customer_ref"]:
                sub.token_ref = token
        auth = ProviderPayment("mock", self._next("pay"), Money(100), PaymentStatus.CAPTURED, "upi", None, None,
                               datetime.now(UTC), customer_ref=reg["customer_ref"], token_ref=token)
        self.payments[auth.provider_payment_id] = auth
        return token, auth

    def charge(self, sub_id: str, *, succeed: bool, error_code: str | None = None,
               at: datetime | None = None, issuer: str | None = None,
               error_reason: str | None = None) -> ProviderPayment:
        """Simulate the provider executing a mandate debit."""
        sub = self.subs[sub_id]
        pay = ProviderPayment(
            provider="mock",
            provider_payment_id=self._next("pay"),
            amount=sub.amount,
            status=PaymentStatus.CAPTURED if succeed else PaymentStatus.FAILED,
            method="upi",
            error_code=None if succeed else (error_code or "BAD_REQUEST_ERROR"),
            error_reason=None if succeed else (error_reason or "insufficient_funds"),
            created_at=at or datetime.now(UTC),
            customer_ref=sub.customer_ref,
            subscription_ref=sub_id,
            token_ref=sub.token_ref,
            issuer=issuer,
        )
        self.payments[pay.provider_payment_id] = pay
        sub.payments.append(pay.provider_payment_id)
        return pay

    def charge_mandate(self, *, token_ref: str, customer_ref: str | None, amount: Money, receipt: str,
                       notes: Mapping[str, str], contact: str | None = None, email: str | None = None,
                       at: datetime | None = None) -> ProviderPayment:
        """A merchant-initiated charge on a mandate (Razorpay: order + recurring payment on the token). The bank's
        answer is scripted by `mandate_outcomes`; a revoked/expired mandate or an amount over its limit is refused
        like the provider would."""
        if any(c["receipt"] == receipt for c in self.mandate_charges):
            raise ProviderRejected(f"duplicate receipt {receipt}")
        m = self.mandates.get(token_ref)
        if m is None or m.status != "active":
            raise ProviderRejected("mandate is not active")
        if m.max_amount is not None and amount.minor > m.max_amount.minor:
            raise ProviderRejected("amount exceeds the mandate limit")
        order = self.create_order(amount, receipt, notes)
        succeed, reason = self.mandate_outcomes.pop(0) if self.mandate_outcomes else (True, None)
        pay = ProviderPayment("mock", self._next("pay"), amount,
                              PaymentStatus.CAPTURED if succeed else PaymentStatus.FAILED, "upi",
                              None if succeed else "BAD_REQUEST_ERROR", None if succeed else (reason or "timeout"),
                              at or datetime.now(UTC), customer_ref=customer_ref, order_ref=order.order_id,
                              notes=dict(notes), token_ref=token_ref)
        self.payments[pay.provider_payment_id] = pay
        self.order_payments.setdefault(order.order_id, []).append(pay.provider_payment_id)
        self.mandate_charges.append({"receipt": receipt, "token": token_ref, "amount_minor": amount.minor,
                                     "payment_id": pay.provider_payment_id})
        return pay

    def open_dispute(self, provider_payment_id: str, reason_code: str = "fraudulent",
                     at: datetime | None = None, respond_days: int = 7) -> ProviderDispute:
        p = self.payments[provider_payment_id]
        at = at or datetime.now(UTC)
        d = ProviderDispute("mock", self._next("disp"), provider_payment_id, p.amount, reason_code, "open",
                            "chargeback", at + timedelta(days=respond_days), at)
        self.disputes[d.provider_dispute_id] = d
        return d

    @staticmethod
    def dispute_entity(d: ProviderDispute) -> dict[str, Any]:
        return {"id": d.provider_dispute_id, "entity": "dispute", "payment_id": d.provider_payment_id,
                "amount": d.amount.minor, "currency": d.amount.currency, "reason_code": d.reason_code,
                "status": d.status, "phase": d.phase,
                "respond_by": int(d.respond_by.timestamp()) if d.respond_by else None,
                "created_at": int(d.created_at.timestamp()) if d.created_at else None}

    def fetch_dispute(self, provider_dispute_id: str) -> ProviderDispute:
        self._guard("fetch_dispute", provider_dispute_id)
        return self.disputes[provider_dispute_id]

    def upload_document(self, filename: str, content: bytes, mime: str, purpose: str = "dispute_evidence") -> str:
        self._guard("upload_document", filename)
        doc_id = self._next("doc")
        self.documents[doc_id] = (mime, len(content))
        return doc_id

    def contest_dispute(self, provider_dispute_id: str, *, summary: str, document_ids: Mapping[str, list[str]],
                        amount: Money | None = None, submit: bool = True) -> ProviderDispute:
        self._guard("contest_dispute", provider_dispute_id)
        if submit and not any(document_ids.values()):
            raise ProviderRejected("mock: at least one document is required to submit")
        self.contests.append({"dispute": provider_dispute_id, "summary": summary, "documents": dict(document_ids),
                              "submit": submit})
        d = self.disputes[provider_dispute_id]
        new = ProviderDispute(d.provider, d.provider_dispute_id, d.provider_payment_id, d.amount, d.reason_code,
                              "submitted" if submit else d.status, d.phase, d.respond_by, d.created_at)
        self.disputes[provider_dispute_id] = new
        return new

    def resolve_dispute(self, provider_dispute_id: str, status: str) -> ProviderDispute:
        """Simulator: the card network / bank decides (won | lost | accepted)."""
        d = self.disputes[provider_dispute_id]
        new = ProviderDispute(d.provider, d.provider_dispute_id, d.provider_payment_id, d.amount, d.reason_code,
                              status, d.phase, d.respond_by, d.created_at)
        self.disputes[provider_dispute_id] = new
        return new

    def mandate_from_webhook(self, entity: Mapping[str, Any]) -> ProviderMandate:
        from nirantar.payments.providers.razorpay import RazorpayProvider

        m = RazorpayProvider.to_mandate(entity)
        return ProviderMandate("mock", m.provider_token_id, m.customer_ref, m.rail, m.status, m.max_amount,
                               m.valid_until, m.failure_reason)

    def list_mandates(self, customer_ref: str) -> list[ProviderMandate]:
        self._guard("list_mandates", customer_ref)
        # like Razorpay: expired and rejected tokens are not listed
        return [m for m in self.mandates.values() if m.customer_ref == customer_ref
                and m.status not in ("expired", "failed")]

    def create_registration_link(self, *, customer_ref: str | None, name: str, contact: str | None,
                                 email: str | None, rail: str, max_amount: Money, expire_at: datetime, receipt: str,
                                 description: str) -> RegistrationLink:
        self._guard("create_registration_link", receipt)
        for lid, reg in self.registrations.items():            # receipts are unique, like Razorpay invoices
            if reg["receipt"] == receipt:
                return RegistrationLink("mock", lid, f"https://mock.pay/r/{lid}", reg["status"])
        lid = self._next("inv")
        self.registrations[lid] = {"receipt": receipt, "rail": rail, "max_amount": max_amount, "status": "issued",
                                   "customer_ref": customer_ref, "has_contact": bool(contact or email)}
        return RegistrationLink("mock", lid, f"https://mock.pay/r/{lid}", "issued")

    def pay_link(self, link_id: str, at: datetime | None = None) -> ProviderPayment:
        link = self.links[link_id]
        pay = ProviderPayment("mock", self._next("pay"), link.amount, PaymentStatus.CAPTURED, "upi", None, None,
                              at or datetime.now(UTC), notes={"reference_id": link.reference_id})
        self.payments[pay.provider_payment_id] = pay
        self.links[link_id] = PaymentLink("mock", link.link_id, link.url, link.amount, "paid", link.reference_id)
        self.link_payments.setdefault(link_id, []).append(pay.provider_payment_id)
        return pay

    def create_order(self, amount: Money, receipt: str, notes: Mapping[str, str]) -> CheckoutOrder:
        order = CheckoutOrder("mock", self._next("order"), amount, receipt[:40], "created")
        self.orders[order.order_id] = order
        return order

    def fetch_order_payments(self, order_id: str) -> list[ProviderPayment]:
        return [self.payments[p] for p in self.order_payments.get(order_id, [])]

    def _checkout_signature(self, order_id: str, payment_id: str) -> str:
        return hmac.new(self.webhook_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

    def verify_checkout(self, order_id: str, payment_id: str, signature: str) -> bool:
        return hmac.compare_digest(self._checkout_signature(order_id, payment_id), signature or "")

    def pay_order(self, order_id: str, at: datetime | None = None) -> tuple[ProviderPayment, str]:
        """Simulate a customer paying in the hosted checkout: returns the payment and Razorpay-style signature."""
        order = self.orders[order_id]
        # like Razorpay: the payment carries the order id; the order's notes are NOT guaranteed to be copied
        pay = ProviderPayment("mock", self._next("pay"), order.amount, PaymentStatus.CAPTURED, "upi", None, None,
                              at or datetime.now(UTC), order_ref=order_id)
        self.payments[pay.provider_payment_id] = pay
        self.order_payments.setdefault(order_id, []).append(pay.provider_payment_id)
        self.orders[order_id] = CheckoutOrder("mock", order_id, order.amount, order.receipt, "paid")
        return pay, self._checkout_signature(order_id, pay.provider_payment_id)

    def fetch_payment_link(self, link_id: str) -> LinkStatus:
        link = self.links[link_id]
        return LinkStatus(link_id, link.status, tuple(self.link_payments.get(link_id, [])))

    def webhook_for(self, provider_type: str, entity_key: str, entity: Mapping[str, Any],
                    at: datetime | None = None) -> tuple[dict[str, str], bytes]:
        """Build a signed Razorpay-format webhook (headers, raw body)."""
        body = json.dumps(
            {"entity": "event", "event": provider_type, "payload": {entity_key: {"entity": dict(entity)}},
             "created_at": int((at or datetime.now(UTC)).timestamp())},
            separators=(",", ":"),
        ).encode()
        sig = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Razorpay-Signature": sig, "x-razorpay-event-id": self._next("evt")}, body

    @staticmethod
    def payment_entity(p: ProviderPayment) -> dict[str, Any]:
        return {
            "id": p.provider_payment_id, "amount": p.amount.minor, "currency": p.amount.currency,
            "status": "captured" if p.status == PaymentStatus.CAPTURED else p.status.value,
            "method": p.method, "error_code": p.error_code, "error_reason": p.error_reason,
            "created_at": int(p.created_at.timestamp()) if p.created_at else None,
            "customer_id": p.customer_ref, "token_id": p.token_ref, "order_id": p.order_ref, "bank": p.issuer,
            "notes": {"subscription_id": p.subscription_ref, **dict(p.notes)} if p.subscription_ref
            else dict(p.notes),
        }

    # ---- PaymentProvider -------------------------------------------------------
    def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        self._guard("fetch_payment", provider_payment_id)
        if provider_payment_id not in self.payments:
            raise ProviderRejected("mock: payment not found", http_status=404)
        return self.payments[provider_payment_id]

    def list_payments(self, since: datetime, until: datetime) -> Iterator[ProviderPayment]:
        self._guard("list_payments", (since, until))
        for p in self.payments.values():
            if p.created_at is not None and since <= p.created_at <= until:
                yield p

    def create_payment_link(self, request: LinkRequest) -> PaymentLink:
        self._guard("create_payment_link", request.reference_id)
        for link in self.links.values():
            if link.reference_id == request.reference_id:
                return link  # idempotent on reference_id
        link = PaymentLink("mock", self._next("plink"), f"https://mock.pay/{request.reference_id}",
                           request.amount, "created", request.reference_id)
        self.links[link.link_id] = link
        return link

    def _sub(self, sub_id: str) -> ProviderSubscription:
        s = self.subs[sub_id]
        return ProviderSubscription("mock", s.sub_id, s.status, None, s.customer_ref, None, None,
                                    sum(1 for p in s.payments if self.payments[p].status == PaymentStatus.CAPTURED),
                                    None)

    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        self._guard("fetch_subscription", provider_subscription_id)
        return self._sub(provider_subscription_id)

    def list_subscription_payments(self, provider_subscription_id: str) -> list[ProviderPayment]:
        self._guard("list_subscription_payments", provider_subscription_id)
        return [self.payments[p] for p in self.subs[provider_subscription_id].payments]

    def pause_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        self._guard("pause_subscription", provider_subscription_id)
        self.subs[provider_subscription_id].status = SubscriptionStatus.PAUSED
        return self._sub(provider_subscription_id)

    def resume_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        self._guard("resume_subscription", provider_subscription_id)
        self.subs[provider_subscription_id].status = SubscriptionStatus.ACTIVE
        return self._sub(provider_subscription_id)

    def create_refund(self, provider_payment_id: str, amount: Money, idempotency_key: str) -> Refund:
        self._guard("create_refund", (provider_payment_id, idempotency_key))
        if idempotency_key in self.refunds:
            return self.refunds[idempotency_key]
        payment = self.fetch_payment(provider_payment_id)
        if payment.status != PaymentStatus.CAPTURED or amount > payment.amount:
            raise ProviderRejected("mock: refund not allowed")
        refund = Refund("mock", self._next("rfnd"), provider_payment_id, amount, "processed")
        self.refunds[idempotency_key] = refund
        return refund

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        return verify_razorpay_signature(headers, raw_body, secret)

    def parse_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> NormalizedWebhook:
        return parse_razorpay_webhook(headers, raw_body, provider="mock")
