"""Money as integer minor units (paise for INR). Floats are rejected everywhere.

This is the only place money arithmetic happens. LLM output is never allowed to
construct a Money for a money action; see BB-§8.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from nirantar.core.errors import MoneyError

# ISO 4217 minor-unit exponents for currencies we support.
_EXPONENT: dict[str, int] = {"INR": 2, "USD": 2, "EUR": 2, "GBP": 2}


@dataclass(frozen=True, slots=True, order=False)
class Money:
    minor: int
    currency: str = "INR"

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise MoneyError(f"minor units must be int, got {type(self.minor).__name__}")
        if self.currency not in _EXPONENT:
            raise MoneyError(f"unsupported currency {self.currency!r}")

    # ---- construction -------------------------------------------------
    @classmethod
    def of(cls, amount: str | Decimal | int, currency: str = "INR") -> Money:
        """Build from major units, e.g. Money.of("999.00") -> 99900 paise."""
        if isinstance(amount, float):
            raise MoneyError("float amounts are not allowed; pass str or Decimal")
        if currency not in _EXPONENT:
            raise MoneyError(f"unsupported currency {currency!r}")
        try:
            dec = Decimal(amount) if not isinstance(amount, Decimal) else amount
        except (InvalidOperation, ValueError) as exc:
            raise MoneyError(f"not a number: {amount!r}") from exc
        if not dec.is_finite():
            raise MoneyError("amount must be finite")
        scaled = dec.scaleb(_EXPONENT[currency])
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"{amount!r} has more precision than {currency} allows")
        return cls(int(scaled), currency)

    @classmethod
    def zero(cls, currency: str = "INR") -> Money:
        return cls(0, currency)

    # ---- arithmetic ---------------------------------------------------
    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError("can only combine Money with Money")
        if other.currency != self.currency:
            raise MoneyError(f"currency mismatch {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def times(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyError("multiply only by int; use allocate() for ratios")
        return Money(self.minor * factor, self.currency)

    def allocate(self, ratios: list[int]) -> list[Money]:
        """Split without losing a paisa. Remainder goes to the earliest shares.

        allocate([1, 1]) of ₹0.03 -> [₹0.02, ₹0.01]
        """
        if not ratios or any(isinstance(r, bool) or not isinstance(r, int) or r < 0 for r in ratios):
            raise MoneyError("ratios must be non-negative ints")
        total = sum(ratios)
        if total == 0:
            raise MoneyError("ratios sum to zero")
        sign = -1 if self.minor < 0 else 1
        magnitude = abs(self.minor)
        shares = [magnitude * r // total for r in ratios]
        remainder = magnitude - sum(shares)
        for i in range(remainder):
            shares[i % len(shares)] += 1
        return [Money(sign * s, self.currency) for s in shares]

    # ---- comparison ---------------------------------------------------
    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.minor >= other.minor

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_positive(self) -> bool:
        return self.minor > 0

    # ---- presentation -------------------------------------------------
    def to_decimal(self) -> Decimal:
        return Decimal(self.minor).scaleb(-_EXPONENT[self.currency])

    def __str__(self) -> str:
        return f"{self.currency} {self.to_decimal():.{_EXPONENT[self.currency]}f}"
