"""Mandate retry sequencer (ADR-0029): when — and whether — to charge a failed mandate debit again.

Never blind. A retry only happens when the failure reason says a later attempt can succeed:
  BANK_TECHNICAL       the bank or rail had a problem → retry soon, later if an incident is still open
  INSUFFICIENT_FUNDS   the money is not there yet → retry in the early morning a few days later, or on the 1st of
                       next month (salary credit) when that is close
  everything else      (revoked / expired mandate, over the mandate limit, card problems, unknown) → never retry:
                       repeating cannot succeed; repair the mandate or ask the customer instead
Every retry is preceded by a pre-debit notice at least NOTICE_LEAD before the charge (RBI e-mandate framework), and the
total number of attempts (the first charge included) is capped. Charges happen only in the early-morning window,
when overnight credits have landed and before the day's spending.
Pure: no I/O, so the same inputs always give the same plan (and the workflow stays deterministic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
NOTICE_LEAD = timedelta(hours=24, minutes=10)     # RBI: notify ≥24h before the debit (+ margin for delivery)
MAX_ATTEMPTS = 3                                  # the first charge + at most two retries
CHARGE_WINDOW = (time(6, 0), time(9, 0))          # IST
RETRYABLE = frozenset({"BANK_TECHNICAL", "INSUFFICIENT_FUNDS"})
NEVER = {
    "MANDATE_REVOKED": "the customer revoked the mandate: a retry cannot succeed; ask them to re-authorise",
    "CARD_EXPIRED": "the card expired: a retry cannot succeed; ask the customer to update it",
    "LIMIT_EXCEEDED": "the amount is above the mandate limit: a retry cannot succeed; re-authorise a higher limit",
    "NOT_PAID": "not a mandate charge failure",
    "UNKNOWN": "the failure reason is unknown: not retried blindly; the customer is contacted instead",
}


@dataclass(frozen=True)
class RetryPlan:
    charge_at: datetime | None          # None → do not retry
    reason: str
    attempt: int                         # the attempt number this plan is for (2, 3, …)


def _window_start(d: date) -> datetime:
    return datetime.combine(d, CHARGE_WINDOW[0], IST)


def _first_window_at_or_after(t: datetime) -> datetime:
    local = t.astimezone(IST)
    start, end = _window_start(local.date()), datetime.combine(local.date(), CHARGE_WINDOW[1], IST)
    if local < start:
        return start
    if local < end:
        return local
    return _window_start(local.date() + timedelta(days=1))


def plan(category: str, attempts_made: int, failed_at: datetime, *, bank_incident_open: bool = False,
         max_attempts: int = MAX_ATTEMPTS) -> RetryPlan:
    """The next retry for a mandate debit that has failed `attempts_made` times (the latest at `failed_at`).
    `failed_at` is also when the pre-debit notice for the retry is sent, so the charge is never sooner than
    NOTICE_LEAD after it."""
    nxt = attempts_made + 1
    if category not in RETRYABLE:
        return RetryPlan(None, NEVER.get(category, f"{category}: not retryable"), nxt)
    if attempts_made >= max_attempts:
        return RetryPlan(None, f"attempt limit reached ({attempts_made}/{max_attempts}); no further charges", nxt)
    earliest = failed_at + NOTICE_LEAD
    local = failed_at.astimezone(IST)
    if category == "BANK_TECHNICAL":
        # the bank's problem, not the customer's: the first morning after the notice lead; a still-open incident
        # pushes it one more day so the retry does not land in the same outage
        target = earliest + (timedelta(days=1) if bank_incident_open else timedelta(0))
        reason = "bank-side failure: retry after the notice period" + (", past the open bank incident"
                                                                         if bank_incident_open else "")
    else:
        # insufficient funds: next month's 1st (salary credit) if it is within 6 days, else 3 days on
        first = (local.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        if (first - local.date()).days <= 6:
            target, reason = max(_window_start(first), earliest), "insufficient funds: retry on the 1st (salary day)"
        else:
            target = max(_window_start(local.date() + timedelta(days=3)), earliest)
            reason = "insufficient funds: retry three days later, early morning"
    return RetryPlan(_first_window_at_or_after(max(target, earliest)), reason, nxt)
