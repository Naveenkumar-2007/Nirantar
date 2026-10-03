"""Immutable double-entry ledger (BB-§34).

- Entries are balanced (Σ debits == Σ credits), single-currency, append-only.
- Posting is idempotent on (tenant_id, idempotency_key): same body -> same entry,
  different body -> IdempotencyConflict.
- Corrections happen only through compensating (reversal) entries.

Nirantar does not hold funds. The ledger records what Nirantar *expects* and what
it has *verified* against providers (receivables, provider clearing, recovered
revenue), so reconciliation and incrementality reporting are auditable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nirantar.core.canonical import sha256_hex
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.errors import IdempotencyConflict, LedgerError, TenantIsolationError
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.core.tenancy import validate_tenant_id


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"

    @property
    def debit_normal(self) -> bool:
        return self in (AccountType.ASSET, AccountType.EXPENSE)


@dataclass(frozen=True, slots=True)
class Account:
    tenant_id: str
    code: str  # e.g. "receivable:subscriptions", "clearing:razorpay"
    type: AccountType
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class Line:
    account_code: str
    debit: Money | None = None
    credit: Money | None = None

    def __post_init__(self) -> None:
        if (self.debit is None) == (self.credit is None):
            raise LedgerError("a line must have exactly one of debit or credit")
        amount = self.debit or self.credit
        assert amount is not None
        if not amount.is_positive:
            raise LedgerError("line amounts must be positive")

    @property
    def amount(self) -> Money:
        amount = self.debit or self.credit
        assert amount is not None
        return amount


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entry_id: str
    tenant_id: str
    idempotency_key: str
    lines: tuple[Line, ...]
    memo: str
    effective_at: datetime
    posted_at: datetime
    provider_ref: str | None = None
    reverses: str | None = None
    body_hash: str = field(default="")


def _body_hash(
    tenant_id: str, lines: Sequence[Line], memo: str, provider_ref: str | None, reverses: str | None
) -> str:
    return sha256_hex(
        {
            "tenant_id": tenant_id,
            "lines": [
                {
                    "account": ln.account_code,
                    "side": "D" if ln.debit is not None else "C",
                    "amount": ln.amount,
                }
                for ln in lines
            ],
            "memo": memo,
            "provider_ref": provider_ref,
            "reverses": reverses,
        }
    )


class LedgerStore(Protocol):
    def get_account(self, tenant_id: str, code: str) -> Account | None: ...
    def add_account(self, account: Account) -> None: ...
    def find_by_key(self, tenant_id: str, key: str) -> JournalEntry | None: ...
    def get_entry(self, tenant_id: str, entry_id: str) -> JournalEntry | None: ...
    def find_reversal_of(self, tenant_id: str, entry_id: str) -> JournalEntry | None: ...
    def insert_entry(self, entry: JournalEntry) -> None: ...
    def entries(self, tenant_id: str) -> list[JournalEntry]: ...
    def account_totals(self, tenant_id: str, code: str) -> tuple[int, int]:
        """(total debits, total credits) in minor units for one account."""
        ...


class InMemoryLedgerStore:
    """Reference store for tests and simulation. The SQL store has the same contract."""

    def __init__(self) -> None:
        self._accounts: dict[tuple[str, str], Account] = {}
        self._entries: dict[str, list[JournalEntry]] = {}

    def get_account(self, tenant_id: str, code: str) -> Account | None:
        return self._accounts.get((tenant_id, code))

    def add_account(self, account: Account) -> None:
        key = (account.tenant_id, account.code)
        if key in self._accounts:
            raise LedgerError(f"account {account.code} already exists")
        self._accounts[key] = account

    def find_by_key(self, tenant_id: str, key: str) -> JournalEntry | None:
        return next((e for e in self._entries.get(tenant_id, []) if e.idempotency_key == key), None)

    def get_entry(self, tenant_id: str, entry_id: str) -> JournalEntry | None:
        return next((e for e in self._entries.get(tenant_id, []) if e.entry_id == entry_id), None)

    def find_reversal_of(self, tenant_id: str, entry_id: str) -> JournalEntry | None:
        return next((e for e in self._entries.get(tenant_id, []) if e.reverses == entry_id), None)

    def insert_entry(self, entry: JournalEntry) -> None:
        self._entries.setdefault(entry.tenant_id, []).append(entry)

    def entries(self, tenant_id: str) -> list[JournalEntry]:
        return list(self._entries.get(tenant_id, []))

    def account_totals(self, tenant_id: str, code: str) -> tuple[int, int]:
        debit = credit = 0
        for entry in self._entries.get(tenant_id, []):
            for ln in entry.lines:
                if ln.account_code != code:
                    continue
                if ln.debit is not None:
                    debit += ln.debit.minor
                else:
                    assert ln.credit is not None
                    credit += ln.credit.minor
        return debit, credit


class Ledger:
    def __init__(self, store: LedgerStore, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or SystemClock()

    def open_account(self, account: Account) -> Account:
        validate_tenant_id(account.tenant_id)
        self._store.add_account(account)
        return account

    def post(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        lines: Sequence[Line],
        memo: str,
        effective_at: datetime,
        provider_ref: str | None = None,
        reverses: str | None = None,
    ) -> JournalEntry:
        validate_tenant_id(tenant_id)
        if not idempotency_key:
            raise LedgerError("idempotency_key is required")
        body_hash = _body_hash(tenant_id, lines, memo, provider_ref, reverses)
        existing = self._store.find_by_key(tenant_id, idempotency_key)
        if existing is not None:
            if existing.body_hash != body_hash:
                raise IdempotencyConflict(f"key {idempotency_key!r} reused with a different entry")
            return existing
        self._validate(tenant_id, lines)
        entry = JournalEntry(
            entry_id=new_id("je"),
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            lines=tuple(lines),
            memo=memo,
            effective_at=effective_at,
            posted_at=self._clock.now(),
            provider_ref=provider_ref,
            reverses=reverses,
            body_hash=body_hash,
        )
        self._store.insert_entry(entry)
        return entry

    def reverse(self, *, tenant_id: str, entry_id: str, idempotency_key: str, reason: str) -> JournalEntry:
        original = self._store.get_entry(tenant_id, entry_id)
        if original is None:
            raise LedgerError(f"entry {entry_id} not found")
        if original.reverses is not None:
            raise LedgerError("cannot reverse a reversal; post a new entry instead")
        prior = self._store.find_reversal_of(tenant_id, entry_id)
        if prior is not None and prior.idempotency_key != idempotency_key:
            raise LedgerError(f"entry {entry_id} already reversed by {prior.entry_id}")
        flipped = [
            Line(ln.account_code, debit=ln.credit, credit=ln.debit) for ln in original.lines
        ]
        return self.post(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            lines=flipped,
            memo=f"reversal of {entry_id}: {reason}",
            effective_at=self._clock.now(),
            provider_ref=original.provider_ref,
            reverses=entry_id,
        )

    def balance(self, tenant_id: str, account_code: str) -> Money:
        """Signed balance in the account's normal direction."""
        account = self._store.get_account(tenant_id, account_code)
        if account is None:
            raise LedgerError(f"unknown account {account_code}")
        debit, credit = self._store.account_totals(tenant_id, account_code)
        signed = debit - credit if account.type.debit_normal else credit - debit
        return Money(signed, account.currency)

    def trial_balance_ok(self, tenant_id: str) -> bool:
        debits = credits = 0
        for entry in self._store.entries(tenant_id):
            for ln in entry.lines:
                if ln.debit is not None:
                    debits += ln.debit.minor
                else:
                    assert ln.credit is not None
                    credits += ln.credit.minor
        return debits == credits

    def _validate(self, tenant_id: str, lines: Sequence[Line]) -> None:
        if len(lines) < 2:
            raise LedgerError("an entry needs at least two lines")
        currencies = {ln.amount.currency for ln in lines}
        if len(currencies) != 1:
            raise LedgerError("entries must be single-currency")
        currency = currencies.pop()
        debit = sum((ln.debit.minor for ln in lines if ln.debit is not None), 0)
        credit = sum((ln.credit.minor for ln in lines if ln.credit is not None), 0)
        if debit != credit:
            raise LedgerError(f"unbalanced entry: debits {debit} != credits {credit}")
        for ln in lines:
            account = self._store.get_account(tenant_id, ln.account_code)
            if account is None:
                raise LedgerError(f"unknown account {ln.account_code}")
            if account.tenant_id != tenant_id:
                raise TenantIsolationError("line references another tenant's account")
            if account.currency != currency:
                raise LedgerError(f"account {account.code} is {account.currency}, entry is {currency}")
