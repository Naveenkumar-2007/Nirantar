"""Resilient HTTP for provider calls: timeouts, bounded retries with jittered
backoff, and a circuit breaker per provider (BB-§37).

Only requests marked `idempotent=True` are retried. A POST is idempotent only
when the provider dedupes it (idempotency header, unique reference/receipt).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from nirantar.payments.domain import (
    ProviderAuthError,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
)

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_after_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = 0
    _opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self.clock() - self._opened_at >= self.reset_after_s:
            return True  # half-open: let one request probe
        return False

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()

    @property
    def is_open(self) -> bool:
        return not self.allow()


@dataclass
class ProviderHttp:
    provider: str
    client: httpx.Client
    max_attempts: int = 3
    base_delay_s: float = 0.2
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    sleep: Callable[[float], None] = time.sleep
    error_parser: Callable[[httpx.Response], tuple[str | None, str]] = lambda r: (None, r.text[:300])

    def request(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        files: Any = None,
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        attempts = self.max_attempts if idempotent else 1
        last: ProviderError | None = None
        for attempt in range(1, attempts + 1):
            if not self.breaker.allow():
                raise ProviderUnavailable(f"{self.provider}: circuit open")
            try:
                resp = self.client.request(method, url, json=json, params=params, headers=headers, files=files,
                                           data=data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.breaker.failure()
                last = ProviderUnavailable(f"{self.provider}: transport error {type(exc).__name__}")
            else:
                if resp.status_code < 400:
                    self.breaker.success()
                    return resp.json() if resp.content else {}
                code, message = self.error_parser(resp)
                if resp.status_code in (401, 403):
                    raise ProviderAuthError(f"{self.provider}: {message}", code=code, http_status=resp.status_code)
                if resp.status_code in RETRYABLE_STATUS:
                    self.breaker.failure()
                    last = ProviderUnavailable(
                        f"{self.provider}: {message}", code=code, http_status=resp.status_code
                    )
                else:
                    self.breaker.success()  # the provider is healthy; the request was rejected
                    raise ProviderRejected(f"{self.provider}: {message}", code=code, http_status=resp.status_code)
            if attempt < attempts:
                delay = self.base_delay_s * (2 ** (attempt - 1))
                self.sleep(delay + random.uniform(0, delay / 2))  # noqa: S311 - jitter, not crypto
        assert last is not None
        raise last
