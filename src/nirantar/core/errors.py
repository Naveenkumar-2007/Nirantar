"""Typed error hierarchy. Callers match on type, never on message text."""


class NirantarError(Exception):
    """Base class for all expected, domain-level failures."""


class MoneyError(NirantarError):
    """Invalid money value or operation (e.g. mixing currencies, float input)."""


class TenantContextMissing(NirantarError):
    """Code that needs a tenant ran outside a tenant scope."""


class TenantIsolationError(NirantarError):
    """A record or request crossed tenant boundaries."""


class IdempotencyConflict(NirantarError):
    """The same idempotency key was reused with a different request body."""


class LedgerError(NirantarError):
    """An entry violates double-entry invariants or immutability rules."""


class AuditChainBroken(NirantarError):
    """Audit chain verification failed: a record was altered, removed or reordered."""
