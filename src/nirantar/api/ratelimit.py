"""Per-client rate limits for the API's unauthenticated surfaces (P8.4): the customer pay page and provider webhooks.

A sliding one-minute window per (rule, client IP), in process — correct for the single API instance of a pilot
deployment. Several API replicas need a shared store (Redis) instead; the rule table stays the same.
The client IP is what uvicorn reports after `--proxy-headers` (set by Caddy), never a header the client controls
directly in production.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    prefix: str
    per_minute: int
    method: str | None = None


RULES = (
    Rule("/v1/public/pay/", 10, "POST"),       # payment confirmations: a person pays once, retries are rare
    Rule("/v1/public/", 60),                   # viewing a pay page
    Rule("/webhooks/", 1200),                  # provider bursts are legitimate; signatures are checked anyway
)


@dataclass
class RateLimiter:
    rules: tuple[Rule, ...] = RULES
    window_s: float = 60.0
    _hits: dict[tuple[str, str], deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def rule_for(self, path: str, method: str) -> Rule | None:
        return next((r for r in self.rules if path.startswith(r.prefix) and r.method in (None, method)), None)

    def allow(self, path: str, method: str, client: str, now: float | None = None) -> tuple[bool, int]:
        """(allowed, seconds until a slot frees). Paths without a rule are not limited here."""
        rule = self.rule_for(path, method)
        if rule is None:
            return True, 0
        t = time.monotonic() if now is None else now
        q = self._hits[(rule.prefix + (rule.method or ""), client)]
        while q and q[0] <= t - self.window_s:
            q.popleft()
        if len(q) >= rule.per_minute:
            return False, max(1, int(q[0] + self.window_s - t) + 1)
        q.append(t)
        if len(self._hits) > 50_000:                 # bound memory under a wide spray of client addresses
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= t - self.window_s][:25_000]:
                del self._hits[k]
        return True, 0
