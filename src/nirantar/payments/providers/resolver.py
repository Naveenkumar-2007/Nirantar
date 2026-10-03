"""Per-tenant provider resolution (P5, ADR-0015).

Every tenant connects its OWN provider account (core.provider_accounts). Activities, webhooks and reconciliation
must re-fetch and act with that tenant's credentials — never a process-wide key. Credentials are secret
references resolved at use time (`env:NAME`, later a vault), cached briefly so rotation takes effect.

secret_ref formats:  razorpay  '<key_id ref>;<key_secret ref>'      cashfree  '<client_id ref>;<client_secret ref>'
The `mock` provider exists only inside a process that injects one (tests, demo, soak) — it is never built from
configuration, so production can't silently run against a simulator.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from nirantar.db.session import tenant_tx
from nirantar.payments.domain import PaymentProvider
from nirantar.security.secrets import resolve_secret

CACHE_TTL_S = 300.0


class ProviderNotConfigured(LookupError):
    pass


def _pair(ref: str, tenant_id: str | None, engine: Any) -> tuple[str, str]:
    a, _, b = ref.partition(";")
    if not a or not b:
        raise ProviderNotConfigured("secret_ref must be '<id ref>;<secret ref>'")
    return (resolve_secret(a, tenant_id=tenant_id, engine=engine),
            resolve_secret(b, tenant_id=tenant_id, engine=engine))


def build_from_account(provider: str, secret_ref: str, tenant_id: str | None = None,
                       engine: Any = None) -> PaymentProvider:
    if provider == "razorpay":
        from nirantar.payments.providers.razorpay import RazorpayProvider

        return RazorpayProvider(*_pair(secret_ref, tenant_id, engine))
    if provider == "cashfree":
        from nirantar.payments.providers.cashfree import CashfreeProvider

        cid, secret = _pair(secret_ref, tenant_id, engine)
        return CashfreeProvider(cid, secret, sandbox=os.environ.get("NIRANTAR_ENV", "local") != "production")
    raise ProviderNotConfigured(f"provider {provider!r} cannot be built from configuration")


@dataclass
class ProviderResolver:
    engine: Engine
    injected: dict[str, PaymentProvider] = field(default_factory=dict)   # e.g. {"mock": MockProvider()}
    _cache: dict[tuple[str, str], tuple[float, PaymentProvider]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def for_tenant(self, tenant_id: str, provider: str | None = None) -> PaymentProvider:
        mode = "live" if os.environ.get("NIRANTAR_ENV") == "production" else "test"
        with tenant_tx(tenant_id, self.engine) as c:
            rows = c.execute(text("SELECT provider, mode, secret_ref FROM core.provider_accounts WHERE tenant_id=:t "
                                  "AND (CAST(:p AS text) IS NULL OR provider=:p) ORDER BY (mode=:m) DESC, created_at"),
                             {"t": tenant_id, "p": provider, "m": mode}).all()
        if not rows:
            raise ProviderNotConfigured(f"tenant {tenant_id} has no {provider or 'payment'} provider connected")
        acct = rows[0]
        if acct.provider in self.injected:
            return self.injected[acct.provider]
        key = (tenant_id, acct.provider)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
                return hit[1]
        built = build_from_account(acct.provider, acct.secret_ref, tenant_id, self.engine)
        with self._lock:
            self._cache[key] = (time.monotonic(), built)
        return built


def provider_for(source: Any, tenant_id: str, provider: str | None = None) -> PaymentProvider:
    """Accept either a resolver (services) or a fixed provider instance (tests)."""
    if isinstance(source, ProviderResolver):
        return source.for_tenant(tenant_id, provider)
    assert source is not None, "no payment provider configured"
    p: PaymentProvider = source
    return p
