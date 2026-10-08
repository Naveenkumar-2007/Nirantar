"""The mandate retry sequencer never retries blindly, always leaves the notice lead, respects the cap and the
morning window (ADR-0029)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nirantar.mandates.retry import IST, MAX_ATTEMPTS, NOTICE_LEAD, plan

FAILED = datetime(2026, 10, 14, 2, 20, tzinfo=IST)          # a Wednesday-night (02:20 IST) debit failure


@pytest.mark.parametrize("category", ["MANDATE_REVOKED", "CARD_EXPIRED", "LIMIT_EXCEEDED", "UNKNOWN", "NOT_PAID",
                                      "SOMETHING_NEW"])
def test_failures_that_cannot_succeed_are_never_retried(category: str) -> None:
    p = plan(category, 1, FAILED)
    assert p.charge_at is None and p.reason


def test_bank_failure_retries_the_first_morning_after_the_notice_lead() -> None:
    p = plan("BANK_TECHNICAL", 1, FAILED)
    assert p.charge_at is not None and p.attempt == 2
    assert p.charge_at - FAILED >= NOTICE_LEAD
    assert p.charge_at == datetime(2026, 10, 15, 6, 0, tzinfo=IST)   # notice lead ends 15th 02:30 → 06:00 window
    assert 6 <= p.charge_at.astimezone(IST).hour < 9


def test_an_open_bank_incident_pushes_the_retry_a_day() -> None:
    calm, incident = plan("BANK_TECHNICAL", 1, FAILED), plan("BANK_TECHNICAL", 1, FAILED, bank_incident_open=True)
    assert calm.charge_at and incident.charge_at and incident.charge_at - calm.charge_at >= timedelta(hours=23)


def test_insufficient_funds_waits_days_or_for_the_salary_day() -> None:
    mid_month = plan("INSUFFICIENT_FUNDS", 1, FAILED)
    assert mid_month.charge_at == datetime(2026, 10, 17, 6, 0, tzinfo=IST)            # 3 days later, 06:00
    month_end = plan("INSUFFICIENT_FUNDS", 1, datetime(2026, 10, 28, 3, 0, tzinfo=IST))
    assert month_end.charge_at == datetime(2026, 11, 1, 6, 0, tzinfo=IST) and "salary" in month_end.reason


def test_the_attempt_cap_is_absolute() -> None:
    assert plan("BANK_TECHNICAL", MAX_ATTEMPTS, FAILED).charge_at is None
    assert plan("INSUFFICIENT_FUNDS", MAX_ATTEMPTS - 1, FAILED).charge_at is not None


def test_every_plan_is_in_the_morning_window_and_after_the_notice() -> None:
    for hour in range(24):
        failed = datetime(2026, 10, 20, hour, 15, tzinfo=IST)
        for cat in ("BANK_TECHNICAL", "INSUFFICIENT_FUNDS"):
            p = plan(cat, 1, failed)
            assert p.charge_at is not None and p.charge_at - failed >= NOTICE_LEAD
            assert 6 <= p.charge_at.astimezone(IST).hour < 9
