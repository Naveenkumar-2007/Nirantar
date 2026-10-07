from datetime import date

import pytest

from nirantar.agents.promise import extract_promise_date

TODAY = date(2026, 10, 7)          # a Wednesday


@pytest.mark.parametrize(("text", "expected"), [
    ("I will pay tomorrow", date(2026, 10, 8)),
    ("kal pay kar dunga bhai", date(2026, 10, 8)),
    ("repu kadatanu", date(2026, 10, 8)),
    ("कल भुगतान कर दूंगा", date(2026, 10, 8)),
    ("రేపు చెల్లిస్తాను", date(2026, 10, 8)),
    ("parso pakka", date(2026, 10, 9)),
    ("ellundi kadatha", date(2026, 10, 9)),
    ("will pay today evening", date(2026, 10, 7)),
    ("aaj shaam tak", date(2026, 10, 7)),
    ("Friday ko pay karunga", date(2026, 10, 9)),
    ("shukravar tak", date(2026, 10, 9)),
    ("wednesday", date(2026, 10, 14)),                 # today is Wednesday: "Wednesday" means next week's
    ("15 tarikh ko", date(2026, 10, 15)),
    ("on the 3rd", date(2026, 11, 3)),                 # already past this month → next month
    ("in 5 days", date(2026, 10, 12)),
    ("3 din me de dunga", date(2026, 10, 10)),
    ("salary aane pe pay karunga", date(2026, 11, 1)),
])
def test_reads_how_people_actually_promise(text: str, expected: date) -> None:
    assert extract_promise_date(text, TODAY) == expected


@pytest.mark.parametrize("text", [
    "I will pay soon",                                  # no date: recorded as a promise without a date
    "will pay ₹499 later",                              # an amount is never a date
    "paying 500 rupees",
    "in 45 days",                                       # beyond the 30-day honour window
    "",
])
def test_no_confident_date_means_none(text: str) -> None:
    assert extract_promise_date(text, TODAY) is None


@pytest.mark.parametrize(("text", "intent"), [
    ("parso pay karunga", "promise_to_pay"), ("kal pay kar dunga", "promise_to_pay"),
    ("payment kar denge friday ko", "promise_to_pay"), ("bhar dunga bhai", "promise_to_pay"),
    ("repu kadatanu", "promise_to_pay"), ("I'll pay on the 15th", "promise_to_pay"),
    ("कल भुगतान कर दूंगा", "promise_to_pay"), ("రేపు చెల్లిస్తాను", "promise_to_pay"),
    ("STOP", "opt_out"), ("lost my job, can't afford it", "hardship"), ("please cancel", "cancel_request"),
    ("who is this?", "other"),
])
def test_reply_intent_understands_hinglish_and_telugu(text: str, intent: str) -> None:
    from nirantar.workflows.activities import classify_reply

    assert classify_reply(text) == intent
