"""Collections Strategist (lending): DPD-bucket plan. Deterministic.

Every step it proposes still passes the Compliance Guardian at execution: RBI recovery hours, agent-detail
disclosure before first contact, no third-party contact, and the production launch gate (ADR-0004 g).
Restructuring offers come only from the lender's grid; legal notices are always human-approved drafts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Step = Literal["disclose_agent_details", "reminder_with_link", "voice_reminder", "restructure_offer",
               "agency_handoff_a2a", "legal_notice_draft", "hardship_review"]


@dataclass(frozen=True)
class LoanCase:
    loan_id: str
    dpd: int
    emi_minor: int
    agent_disclosed: bool
    hardship_flag: bool
    grid_allows_restructure: bool


@dataclass(frozen=True)
class CollectionsPlan:
    bucket: str
    steps: tuple[Step, ...]
    human_review: bool
    rationale: str


def bucket(dpd: int) -> str:
    return "current" if dpd <= 0 else "1-30" if dpd <= 30 else "31-60" if dpd <= 60 else "61-90" if dpd <= 90 else "90+"


def plan(case: LoanCase) -> CollectionsPlan:
    b = bucket(case.dpd)
    if case.hardship_flag:
        return CollectionsPlan(b, ("hardship_review",), True,
                               "hardship reported: pause collection contact, human review")
    steps: list[Step] = [] if case.agent_disclosed else ["disclose_agent_details"]
    extra: list[Step]
    if b == "current":
        return CollectionsPlan(b, (), False, "no dues")
    if b == "1-30":
        extra = ["reminder_with_link"]
    elif b == "31-60":
        extra = ["voice_reminder", "restructure_offer"] if case.grid_allows_restructure else ["voice_reminder"]
    elif b == "61-90":
        extra = ["voice_reminder", "agency_handoff_a2a"]
    else:
        extra = ["agency_handoff_a2a", "legal_notice_draft"]
    steps += extra
    return CollectionsPlan(b, tuple(steps), "legal_notice_draft" in steps,
                           f"DPD {case.dpd} → bucket {b}; every step is policy-checked at execution")
