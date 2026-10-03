"""Local payment state machine.

Provider events arrive out of order (Razorpay says so explicitly), so local state
only moves forward. `captured` never regresses to `failed`; refunds only follow a
capture. Any provider state that would move backwards is recorded as a
discrepancy for review instead of being applied.
"""

from __future__ import annotations

from nirantar.payments.domain import PaymentStatus

_RANK = {
    PaymentStatus.CREATED: 0,
    PaymentStatus.AUTHORIZED: 1,
    PaymentStatus.FAILED: 2,
    PaymentStatus.CAPTURED: 3,
    PaymentStatus.PARTIALLY_REFUNDED: 4,
    PaymentStatus.REFUNDED: 5,
}

_ALLOWED: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(set(PaymentStatus) - {PaymentStatus.CREATED}),
    PaymentStatus.AUTHORIZED: frozenset({PaymentStatus.CAPTURED, PaymentStatus.FAILED, PaymentStatus.REFUNDED}),
    # A failed attempt can later be captured (late success on UPI); it can't be refunded directly.
    PaymentStatus.FAILED: frozenset({PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED}),
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
}


def transition(current: PaymentStatus | None, incoming: PaymentStatus) -> tuple[PaymentStatus, bool]:
    """Return (new_state, is_regression). Regressions keep the current state."""
    if current is None or current == incoming:
        return incoming, False
    if incoming in _ALLOWED[current]:
        return incoming, False
    return current, True


def rank(status: PaymentStatus) -> int:
    return _RANK[status]
