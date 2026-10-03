"""Verify payments against the provider and settle verified money in the ledger.

"Agent says the customer paid" → fetch the authoritative provider record →
compare status, amount and currency → only then post to the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Connection

from nirantar.core.canonical import sha256_hex
from nirantar.core.money import Money
from nirantar.db.stores import SqlLedgerStore
from nirantar.ledger import Account, AccountType, Ledger, Line
from nirantar.payments.domain import PaymentProvider, PaymentStatus, ProviderError, ProviderPayment

RECEIVABLE = "receivable:recurring"
INCOME = "income:recurring"


def clearing_account(provider: str) -> str:
    return f"clearing:{provider}"


@dataclass(frozen=True)
class Verification:
    verified: bool
    reason: str
    provider_payment_id: str
    observed_status: str | None
    observed_amount_minor: int | None
    evidence_hash: str | None       # hash of the provider record we relied on


def verify_capture(provider: PaymentProvider, provider_payment_id: str, expected: Money) -> Verification:
    try:
        p = provider.fetch_payment(provider_payment_id)
    except ProviderError as exc:
        return Verification(False, f"provider_unreachable: {exc}", provider_payment_id, None, None, None)
    evidence = sha256_hex({"id": p.provider_payment_id, "status": p.status.value, "amount": p.amount})
    if p.status not in (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED):
        return Verification(False, f"status_{p.status.value}", p.provider_payment_id, p.status.value,
                            p.amount.minor, evidence)
    if p.amount.currency != expected.currency:
        return Verification(False, "currency_mismatch", p.provider_payment_id, p.status.value, p.amount.minor,
                            evidence)
    if p.amount.minor != expected.minor:
        return Verification(False, "amount_mismatch", p.provider_payment_id, p.status.value, p.amount.minor,
                            evidence)
    return Verification(True, "captured_and_matched", p.provider_payment_id, p.status.value, p.amount.minor,
                        evidence)


def ensure_accounts(ledger: Ledger, store: SqlLedgerStore, tenant_id: str, provider: str) -> None:
    for code, kind in ((RECEIVABLE, AccountType.ASSET), (INCOME, AccountType.INCOME),
                       (clearing_account(provider), AccountType.ASSET)):
        if store.get_account(tenant_id, code) is None:
            ledger.open_account(Account(tenant_id, code, kind))


def accrue_debit(conn: Connection, tenant_id: str, debit_id: str, amount: Money, provider: str,
                 effective_at: datetime) -> None:
    """When a debit is scheduled: DR receivable, CR income. Idempotent on debit id."""
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, provider)
    ledger.post(tenant_id=tenant_id, idempotency_key=f"accrue:{debit_id}", memo=f"debit scheduled {debit_id}",
                effective_at=effective_at,
                lines=[Line(RECEIVABLE, debit=amount), Line(INCOME, credit=amount)])


def settle_verified(conn: Connection, tenant_id: str, debit_id: str, verification: Verification,
                    payment: ProviderPayment, effective_at: datetime) -> str:
    """Verified capture: DR clearing:<provider>, CR receivable. Refuses unverified input."""
    if not verification.verified:
        raise ValueError(f"cannot settle unverified payment: {verification.reason}")
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, payment.provider)
    entry = ledger.post(
        tenant_id=tenant_id, idempotency_key=f"settle:{payment.provider}:{payment.provider_payment_id}",
        memo=f"verified capture for {debit_id}", effective_at=effective_at,
        provider_ref=payment.provider_payment_id,
        lines=[Line(clearing_account(payment.provider), debit=payment.amount),
               Line(RECEIVABLE, credit=payment.amount)],
    )
    return entry.entry_id


def settle_offer_verified(conn: Connection, tenant_id: str, offer_id: str, verification: Verification,
                          payment: ProviderPayment, effective_at: datetime) -> str:
    """Verified reactivation (win-back) payment: DR clearing:<provider>, CR income. There was no receivable —
    nothing was owed after churn — so the cash is recognised as income directly. Refuses unverified input."""
    if not verification.verified:
        raise ValueError(f"cannot settle unverified payment: {verification.reason}")
    store = SqlLedgerStore(conn)
    ledger = Ledger(store)
    ensure_accounts(ledger, store, tenant_id, payment.provider)
    entry = ledger.post(
        tenant_id=tenant_id, idempotency_key=f"settle:{payment.provider}:{payment.provider_payment_id}",
        memo=f"verified reactivation for {offer_id}", effective_at=effective_at,
        provider_ref=payment.provider_payment_id,
        lines=[Line(clearing_account(payment.provider), debit=payment.amount), Line(INCOME, credit=payment.amount)],
    )
    return entry.entry_id
