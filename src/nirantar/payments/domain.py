"""Provider-neutral payments contract (BB-§10).

Adapters translate provider APIs into these types. Workflows and MCP tools only
ever see these types, and they check `capabilities` instead of assuming that
Razorpay, Cashfree and Stripe behave the same.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from nirantar.core.errors import NirantarError
from nirantar.core.money import Money


class Capability(StrEnum):
    SUBSCRIPTIONS = "subscriptions"
    PAUSE_SUBSCRIPTION = "pause_subscription"
    CHARGE_SUBSCRIPTION = "charge_subscription"      # merchant-initiated charge on a mandate
    PAYMENT_LINKS = "payment_links"
    REFUNDS = "refunds"
    REFUND_IDEMPOTENCY = "refund_idempotency"        # provider dedupes refunds on a merchant key
    REQUEST_IDEMPOTENCY = "request_idempotency"      # generic idempotency-key header
    DISPUTES = "disputes"
    WEBHOOK_TIMESTAMP = "webhook_timestamp"          # signature covers a timestamp (replay protection)
    WEBHOOK_REPLAY = "webhook_replay"                # provider can re-deliver missed events


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class SubscriptionStatus(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    HALTED = "halted"          # provider stopped retrying after repeated failures
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ProviderPayment:
    provider: str
    provider_payment_id: str
    amount: Money
    status: PaymentStatus
    method: str | None
    error_code: str | None
    error_reason: str | None
    created_at: datetime | None
    customer_ref: str | None = None
    subscription_ref: str | None = None
    order_ref: str | None = None
    notes: Mapping[str, str] = field(default_factory=dict)
    token_ref: str | None = None          # the mandate/token the provider debited (Razorpay payment.token_id)


@dataclass(frozen=True, slots=True)
class ProviderMandate:
    """A recurring-payment authorisation as the provider reports it (Razorpay: a token with recurring=true)."""
    provider: str
    provider_token_id: str
    customer_ref: str | None
    rail: str                             # upi_autopay | emandate | card | nach | other
    status: str                           # pending | active | paused | revoked | expired | failed
    max_amount: Money | None
    valid_until: datetime | None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrationLink:
    """A provider-hosted page where the CUSTOMER authorises a new mandate (Nirantar never registers one itself)."""
    provider: str
    link_id: str
    url: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderSubscription:
    provider: str
    provider_subscription_id: str
    status: SubscriptionStatus
    plan_ref: str | None
    customer_ref: str | None
    current_end: datetime | None
    charge_at: datetime | None
    paid_count: int
    remaining_count: int | None


@dataclass(frozen=True, slots=True)
class ProviderDispute:
    provider: str
    provider_dispute_id: str
    provider_payment_id: str
    amount: Money
    reason_code: str
    status: str                  # nirantar vocabulary: open | submitted | won | lost | accepted
    phase: str | None
    respond_by: datetime | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class PaymentLink:
    provider: str
    link_id: str
    url: str
    amount: Money
    status: str
    reference_id: str


@dataclass(frozen=True, slots=True)
class Refund:
    provider: str
    refund_id: str
    provider_payment_id: str
    amount: Money
    status: str


@dataclass(frozen=True, slots=True)
class NormalizedWebhook:
    """Provider webhook mapped to Nirantar's event vocabulary."""

    provider: str
    provider_event_id: str       # dedupe key from the provider
    provider_event_type: str
    event_type: str              # nirantar event type, e.g. "payment.failed"
    occurred_at: datetime | None
    subject_ref: str             # provider id of the main entity
    entities: Mapping[str, Any]  # raw entity payloads (never trusted without a re-fetch)


@dataclass(frozen=True, slots=True)
class LinkRequest:
    amount: Money
    reference_id: str            # Nirantar id; unique per tenant; used for idempotency
    description: str
    customer_name: str | None
    customer_phone: str | None
    customer_email: str | None
    expire_by: datetime | None = None
    notify_sms: bool = False
    notify_email: bool = False


# ---- errors ----------------------------------------------------------------
class ProviderError(NirantarError):
    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    """Timeouts, 5xx, 429, open circuit. Safe to retry later."""

    def __init__(self, message: str, **kw: Any) -> None:
        kw.setdefault("retryable", True)
        super().__init__(message, **kw)


class ProviderRejected(ProviderError):
    """4xx business rejection (bad request, invalid state). Do not blind-retry."""


class ProviderAuthError(ProviderError):
    """Credentials invalid or missing."""


class NotSupported(ProviderError):
    """The adapter does not declare this capability."""


class PaymentProvider(Protocol):
    name: str
    capabilities: frozenset[Capability]

    def fetch_payment(self, provider_payment_id: str) -> ProviderPayment: ...
    def list_payments(self, since: datetime, until: datetime) -> Iterator[ProviderPayment]: ...
    def create_payment_link(self, request: LinkRequest) -> PaymentLink: ...
    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription: ...
    def list_subscription_payments(self, provider_subscription_id: str) -> list[ProviderPayment]: ...
    def pause_subscription(self, provider_subscription_id: str) -> ProviderSubscription: ...
    def resume_subscription(self, provider_subscription_id: str) -> ProviderSubscription: ...
    def create_refund(self, provider_payment_id: str, amount: Money, idempotency_key: str) -> Refund: ...
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool: ...
    def parse_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> NormalizedWebhook: ...


def require(provider: PaymentProvider, capability: Capability) -> None:
    if capability not in provider.capabilities:
        raise NotSupported(f"{provider.name} does not support {capability.value}")


def lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}
