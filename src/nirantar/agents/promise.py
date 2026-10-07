"""Promise-to-pay date extraction (P10): "kal pay karunga", "Friday ko", "15 tarikh tak", "salary aane pe", "repu".

Deterministic rules for English, Hinglish (romanised Hindi) and romanised Telugu, plus the most common Devanagari and
Telugu-script words. An LLM is not needed for the date itself; the intent classifier already decided this IS a
promise. Returns None when no date can be read with confidence — a vague promise is recorded without a date and
the normal recovery cadence continues. A promise further than MAX_DAYS out is not honoured as a pause.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

MAX_DAYS = 30

_TODAY = r"(today|tonight|aaj|aj|ivala|ivvala|ఈరోజు|आज)"
_TOMORROW = r"(tomorrow|tmrw|tmr|kal|kl|repu|రేపు|कल)"
_DAY_AFTER = r"(day after tomorrow|parso|parson|ellundi|ఎల్లుండి|परसों)"
_IN_DAYS = r"(?:in|within|next)\s+(\d{1,2})\s+days?|(\d{1,2})\s+(?:din|dino|days?)\s*(?:me|mein|lo|main)?"
_WEEKDAYS = {
    0: r"(monday|mon|somvar|somwar|सोमवार|సోమవారం)", 1: r"(tuesday|tue|mangalvar|mangalwar|मंगलवार|మంగళవారం)",
    2: r"(wednesday|wed|budhvar|budhwar|बुधवार|బుధవారం)", 3: r"(thursday|thu|guruvar|guruwar|गुरुवार|గురువారం)",
    4: r"(friday|fri|shukravar|shukrawar|शुक्रवार|శుక్రవారం)", 5: r"(saturday|sat|shanivar|shaniwar|शनिवार|శనివారం)",
    6: r"(sunday|sun|ravivar|raviwar|itvar|रविवार|ఆదివారం)",
}
_DAY_OF_MONTH = r"(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s*(?:tarikh|tareekh|tarik|తేదీ|तारीख|date)?"
_SALARY = r"(salary|tankha|tankhwah|jeetham|జీతం|सैलरी|तनख्वाह)"


def _next_weekday(today: date, wd: int) -> date:
    days = (wd - today.weekday()) % 7
    return today + timedelta(days=days or 7)


def _day_of_month(today: date, day: int) -> date | None:
    if not 1 <= day <= 31:
        return None
    y, m = today.year, today.month
    if day < today.day:                       # "15th" said on the 20th means next month's 15th
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def extract_promise_date(text: str, today: date) -> date | None:
    t = (text or "").lower()

    def has(p: str) -> bool:
        return re.search(rf"(?<![\w]){p}(?![\w])", t) is not None

    found: date | None = None
    if has(_DAY_AFTER):
        found = today + timedelta(days=2)
    elif m := re.search(_IN_DAYS, t):
        found = today + timedelta(days=int(m.group(1) or m.group(2)))
    elif has(_TOMORROW):
        found = today + timedelta(days=1)
    elif has(_TODAY):
        found = today
    else:
        for wd, p in _WEEKDAYS.items():
            if has(p):
                found = _next_weekday(today, wd)
                break
        if found is None and (m := re.search(rf"(?<![\d₹.,]){_DAY_OF_MONTH}(?![\d,.])", t)):
            # a bare number is a date only with a date word or ordinal; never an amount ("₹499", "500 rupees")
            word = re.search(r"\d{1,2}\s*(st|nd|rd|th|tarikh|tareekh|tarik|తేదీ|तारीख|date)", t)
            on = re.search(r"\bon\s+(the\s+)?\d{1,2}\b", t)
            if word or on:
                found = _day_of_month(today, int(m.group(1)))
        if found is None and has(_SALARY):
            found = _day_of_month(today, 1) if today.day > 1 else today   # salaries land on the 1st
    if found is None or found < today or (found - today).days > MAX_DAYS:
        return None
    return found
