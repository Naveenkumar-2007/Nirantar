from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nirantar.core.clock import FixedClock
from nirantar.core.errors import IdempotencyConflict, LedgerError, TenantIsolationError
from nirantar.core.money import Money
from nirantar.ledger import Account, AccountType, InMemoryLedgerStore, Ledger, Line

T = "ten_alpha"
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def make_ledger() -> Ledger:
    ledger = Ledger(InMemoryLedgerStore(), FixedClock(NOW))
    ledger.open_account(Account(T, "receivable:subscriptions", AccountType.ASSET))
    ledger.open_account(Account(T, "clearing:razorpay", AccountType.ASSET))
    ledger.open_account(Account(T, "income:subscriptions", AccountType.INCOME))
    ledger.open_account(Account("ten_beta", "clearing:razorpay", AccountType.ASSET))
    return ledger


def charge(ledger: Ledger, key: str, amount: str = "999") -> str:
    entry = ledger.post(
        tenant_id=T,
        idempotency_key=key,
        lines=[
            Line("receivable:subscriptions", debit=Money.of(amount)),
            Line("income:subscriptions", credit=Money.of(amount)),
        ],
        memo="subscription billed",
        effective_at=NOW,
    )
    return entry.entry_id


def test_balanced_entry_updates_balances() -> None:
    ledger = make_ledger()
    charge(ledger, "k1")
    assert ledger.balance(T, "receivable:subscriptions") == Money.of("999")
    assert ledger.balance(T, "income:subscriptions") == Money.of("999")
    assert ledger.trial_balance_ok(T)


def test_unbalanced_and_single_line_entries_rejected() -> None:
    ledger = make_ledger()
    with pytest.raises(LedgerError):
        ledger.post(
            tenant_id=T, idempotency_key="k", memo="x", effective_at=NOW,
            lines=[
                Line("receivable:subscriptions", debit=Money.of("10")),
                Line("income:subscriptions", credit=Money.of("9")),
            ],
        )
    with pytest.raises(LedgerError):
        ledger.post(
            tenant_id=T, idempotency_key="k2", memo="x", effective_at=NOW,
            lines=[Line("receivable:subscriptions", debit=Money.of("10"))],
        )


def test_idempotent_replay_returns_same_entry_and_conflict_is_detected() -> None:
    ledger = make_ledger()
    first = charge(ledger, "same-key")
    assert charge(ledger, "same-key") == first
    with pytest.raises(IdempotencyConflict):
        charge(ledger, "same-key", amount="500")
    assert ledger.balance(T, "receivable:subscriptions") == Money.of("999")


def test_reversal_compensates_and_cannot_be_repeated() -> None:
    ledger = make_ledger()
    eid = charge(ledger, "k1")
    rev = ledger.reverse(tenant_id=T, entry_id=eid, idempotency_key="rev-1", reason="duplicate charge")
    assert rev.reverses == eid
    assert ledger.balance(T, "receivable:subscriptions").is_zero
    # replay of the same reversal is idempotent
    replay = ledger.reverse(tenant_id=T, entry_id=eid, idempotency_key="rev-1", reason="duplicate charge")
    assert replay == rev
    with pytest.raises(LedgerError):
        ledger.reverse(tenant_id=T, entry_id=eid, idempotency_key="rev-2", reason="again")
    with pytest.raises(LedgerError):
        ledger.reverse(tenant_id=T, entry_id=rev.entry_id, idempotency_key="rev-3", reason="nope")


def test_cannot_post_to_unknown_or_other_tenant_account() -> None:
    ledger = make_ledger()
    with pytest.raises(LedgerError):
        ledger.post(
            tenant_id=T, idempotency_key="k", memo="x", effective_at=NOW,
            lines=[
                Line("receivable:subscriptions", debit=Money.of("1")),
                Line("does-not-exist", credit=Money.of("1")),
            ],
        )
    # 'ten_beta' account code exists, but under another tenant -> unknown for T
    assert ledger.balance("ten_beta", "clearing:razorpay").is_zero
    with pytest.raises((LedgerError, TenantIsolationError)):
        ledger.post(
            tenant_id="ten_gamma", idempotency_key="k", memo="x", effective_at=NOW,
            lines=[
                Line("clearing:razorpay", debit=Money.of("1")),
                Line("income:subscriptions", credit=Money.of("1")),
            ],
        )


@given(st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=1, max_size=30))
def test_trial_balance_always_holds(amounts: list[int]) -> None:
    ledger = make_ledger()
    for i, minor in enumerate(amounts):
        ledger.post(
            tenant_id=T, idempotency_key=f"k{i}", memo="x", effective_at=NOW,
            lines=[
                Line("clearing:razorpay", debit=Money(minor)),
                Line("receivable:subscriptions", credit=Money(minor)),
            ],
        )
    assert ledger.trial_balance_ok(T)
    assert ledger.balance(T, "clearing:razorpay") == Money(sum(amounts))
