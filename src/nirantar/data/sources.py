"""Backfill sources: read a tenant's HISTORY from its payment provider, as raw provider-shaped records.

Razorpay endpoints (docs retrieved 2026-09-29, see docs/integrations/razorpay.md § Backfill):
  GET /v1/payments       from, to (unix s), count ≤ 100, skip
  GET /v1/subscriptions  from, to, count ≤ 100, skip (plan_id optional)
  GET /v1/customers      count ≤ 100, skip (from/to accepted)
  GET /v1/invoices?subscription_id=…   invoice ↔ subscription ↔ payment_id, billing_start/billing_end
Credentials come from the tenant's provider account (`core.provider_accounts.secret_ref`), never from code.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol

import httpx

from nirantar.payments.http import ProviderHttp
from nirantar.payments.providers.mock import MockProvider
from nirantar.payments.providers.razorpay import BASE_URL, _error_parser
from nirantar.security.secrets import resolve_secret

ENTITIES = ("customers", "subscriptions", "payments")     # windowed by created_at
PAGE = 100


class BackfillSource(Protocol):
    provider: str

    def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]: ...

    def invoices_for(self, subscription_id: str) -> Iterator[dict[str, Any]]: ...


class RazorpaySource:
    provider = "razorpay"

    def __init__(self, key_id: str, key_secret: str, *, http: ProviderHttp | None = None,
                 base_url: str = BASE_URL) -> None:
        if not key_id or not key_secret:
            raise ValueError("razorpay credentials required")
        self._base = base_url.rstrip("/")
        self._http = http or ProviderHttp(provider="razorpay", error_parser=_error_parser,
                                          client=httpx.Client(auth=(key_id, key_secret), timeout=20.0))

    def _pages(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        skip = 0
        while True:
            page = self._http.request("GET", f"{self._base}/{path}", idempotent=True,
                                      params={**params, "count": PAGE, "skip": skip})
            items = page.get("items", [])
            yield from (dict(i) for i in items)
            if len(items) < PAGE:
                return
            skip += PAGE

    def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        if entity not in ENTITIES:
            raise ValueError(f"unknown entity {entity}")
        yield from self._pages(entity, {"from": int(since.timestamp()), "to": int(until.timestamp())})

    def invoices_for(self, subscription_id: str) -> Iterator[dict[str, Any]]:
        yield from self._pages("invoices", {"subscription_id": subscription_id})


class MockSource:
    """Razorpay-shaped history from the simulator (tests/demo). Invoices are not simulated."""
    provider = "mock"

    def __init__(self, mock: MockProvider) -> None:
        self.mock = mock

    def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        if entity == "payments":
            for p in self.mock.payments.values():
                if p.created_at and since <= p.created_at < until:
                    yield {**MockProvider.payment_entity(p), "entity": "payment"}
        elif entity == "subscriptions":
            for s in self.mock.subs.values():
                times = [t for pid in s.payments if (t := self.mock.payments[pid].created_at) is not None]
                first = min(times) if times else None
                if first and since <= first < until:
                    yield {"id": s.sub_id, "entity": "subscription", "customer_id": s.customer_ref,
                           "status": s.status.value, "created_at": int(first.timestamp()),
                           "paid_count": sum(1 for pid in s.payments if self.mock.payments[pid].status == "captured")}
        elif entity == "customers":
            return
        else:
            raise ValueError(f"unknown entity {entity}")

    def invoices_for(self, subscription_id: str) -> Iterator[dict[str, Any]]:
        return iter(())


def source_for_account(provider: str, secret_ref: str) -> BackfillSource:
    """Build a source from a provider account. Razorpay secret_ref format: '<key_id ref>;<key_secret ref>',
    e.g. 'env:RAZORPAY_KEY_ID;env:RAZORPAY_KEY_SECRET' (OAuth access tokens replace this in P8)."""
    if provider == "razorpay":
        kid_ref, _, secret = secret_ref.partition(";")
        return RazorpaySource(resolve_secret(kid_ref), resolve_secret(secret))
    raise ValueError(f"no backfill source for provider {provider!r}")
