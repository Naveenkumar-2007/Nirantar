from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nirantar.core.errors import MoneyError
from nirantar.core.money import Money


def test_of_parses_major_units_exactly() -> None:
    assert Money.of("999.00").minor == 99900
    assert Money.of(Decimal("0.01")).minor == 1
    assert Money.of(5).minor == 500


@pytest.mark.parametrize("bad", [9.99, "9.999", "abc", "NaN", "Infinity"])
def test_of_rejects_floats_and_excess_precision(bad: object) -> None:
    with pytest.raises(MoneyError):
        Money.of(bad)  # type: ignore[arg-type]


def test_constructor_rejects_non_int_minor() -> None:
    with pytest.raises(MoneyError):
        Money(1.5)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(True)


def test_currency_mismatch_is_an_error() -> None:
    with pytest.raises(MoneyError):
        Money(100, "INR") + Money(100, "USD")
    with pytest.raises(MoneyError):
        _ = Money(100, "INR") < Money(100, "USD")


def test_str_formats_two_decimals() -> None:
    assert str(Money.of("999")) == "INR 999.00"


@given(
    minor=st.integers(min_value=-10**12, max_value=10**12),
    ratios=st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=8).filter(
        lambda r: sum(r) > 0
    ),
)
def test_allocate_never_loses_or_creates_paise(minor: int, ratios: list[int]) -> None:
    shares = Money(minor).allocate(ratios)
    assert sum(s.minor for s in shares) == minor
    assert len(shares) == len(ratios)
