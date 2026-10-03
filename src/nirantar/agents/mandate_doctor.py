"""Mandate Doctor: decide the repair for an unhealthy mandate. Deterministic rules; tools do the work.

Repairs are re-authorisation links the CUSTOMER completes (Nirantar never re-registers a mandate itself;
UPI AutoPay re-registration needs the payer's UPI PIN — NPCI lifecycle rules, IN-NPCI-UPIAUTOPAY-LIFECYCLE-001).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Repair = Literal["none", "reauth_card_expiring", "reauth_revoked", "resume_paused", "raise_limit", "human_review"]


class MandateIn(BaseModel):
    mandate_id: str
    rail: str
    status: str                  # pending | active | paused | revoked | expired | failed
    valid_until: date | None
    max_amount_minor: int | None          # None = the provider did not report a limit
    next_debit_amount_minor: int
    next_debit_on: date
    today: date
    revoked_reason: str | None = None


class MandatePlan(BaseModel):
    repair: Repair
    urgency_days: int | None
    customer_action_needed: bool
    rationale: str


def diagnose(m: MandateIn) -> MandatePlan:
    days_to_debit = (m.next_debit_on - m.today).days
    if m.status == "revoked":
        if (m.revoked_reason or "").lower() in ("fraud", "disputed", "unauthorised", "unauthorized"):
            return MandatePlan(repair="human_review", urgency_days=None, customer_action_needed=False,
                               rationale="revoked for suspected fraud: never nudge, route to a human")
        return MandatePlan(repair="reauth_revoked", urgency_days=days_to_debit, customer_action_needed=True,
                           rationale="mandate revoked by customer/bank: offer re-authorisation (win-back)")
    if m.status == "paused":
        return MandatePlan(repair="resume_paused", urgency_days=days_to_debit, customer_action_needed=True,
                           rationale="customer paused the mandate; next debit will fail unless resumed")
    if m.status in ("expired",) or (m.valid_until and m.valid_until < m.next_debit_on):
        return MandatePlan(repair="reauth_card_expiring", urgency_days=days_to_debit, customer_action_needed=True,
                           rationale="mandate/card validity ends before the next debit")
    if m.valid_until and (m.valid_until - m.today).days <= 30:
        return MandatePlan(repair="reauth_card_expiring", urgency_days=(m.valid_until - m.today).days,
                           customer_action_needed=True, rationale="validity ends within 30 days")
    if m.max_amount_minor is not None and m.max_amount_minor < m.next_debit_amount_minor:
        return MandatePlan(repair="raise_limit", urgency_days=days_to_debit, customer_action_needed=True,
                           rationale="mandate limit is below the next debit amount")
    return MandatePlan(repair="none", urgency_days=None, customer_action_needed=False, rationale="healthy")
