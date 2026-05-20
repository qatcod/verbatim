"""Deadline parsing + due-status classification for the proactive layer.

Commitments carry a free-text `deadline` ("Friday", "EOD Wednesday",
"2026-05-23", "next week"). To track what's overdue or due soon, that text
has to resolve to an actual calendar date. This module does that resolution
with the stdlib only — no dateutil — and stays conservative: when a deadline
is genuinely ambiguous it returns None rather than guessing wrong.

`parse_deadline` is intentionally more capable than the projection layer's
`_parse_deadline_to_iso` (which only accepts strict ISO). Projection feeds an
external tracker's due-date field where a wrong guess is costly; the proactive
layer surfaces nudges where a best-effort guess is useful and easily dismissed.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# How many days ahead still counts as "due soon".
DUE_SOON_WINDOW_DAYS = 7

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Filler phrases stripped before parsing — "by EOD Friday" → "friday".
_STRIP_PREFIXES = (
    "by end of day", "by the end of", "by eod", "by cob", "by close of business",
    "end of day", "close of business", "by end of", "due by", "due", "by",
    "before", "no later than", "nlt", "eod", "cob", "target", "tentatively",
)


def _iso_attempt(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _next_weekday(today: date, target: int) -> date:
    """The next occurrence of weekday `target` strictly after `today`.

    'Friday' said on a Tuesday means this coming Friday; said on a Friday it
    means next Friday (a deadline named for today has almost always slipped).
    """
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def parse_deadline(text: str | None, *, today: date | None = None) -> date | None:
    """Resolve a free-text deadline to a calendar date, or None if unparseable.

    Handles: ISO dates, weekday names ('Friday'), 'today'/'tomorrow'/'tonight',
    'next week'/'this week', 'in N days', 'N days', and month-day forms
    ('May 23', '23 May', 'May 23 2026'). Filler like 'EOD'/'by'/'before' is
    stripped first.
    """
    if not text:
        return None
    today = today or date.today()
    s = text.strip().lower().rstrip(".")
    if not s:
        return None

    iso = _iso_attempt(text.strip())
    if iso is not None:
        return iso

    # Strip leading filler words/phrases (longest first so "by eod" wins over "by").
    changed = True
    while changed:
        changed = False
        for prefix in _STRIP_PREFIXES:
            if s.startswith(prefix + " "):
                s = s[len(prefix) + 1:].strip()
                changed = True
        s = s.strip(" ,")

    if s in ("today", "tonight", "this evening", "eod"):
        return today
    if s in ("tomorrow", "tmrw", "tom"):
        return today + timedelta(days=1)
    if s in ("next week", "end of next week"):
        return today + timedelta(days=7)
    if s in ("this week", "end of week", "end of the week"):
        return _next_weekday(today, 4)  # Friday of the current week-ish

    # "in 3 days" / "3 days" / "in a week"
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s+days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))
    if s in ("in a week", "a week"):
        return today + timedelta(days=7)

    # Weekday name, possibly with "next " prefix.
    next_prefix = False
    weekday_text = s
    if weekday_text.startswith("next "):
        next_prefix = True
        weekday_text = weekday_text[5:].strip()
    if weekday_text in _WEEKDAYS:
        result = _next_weekday(today, _WEEKDAYS[weekday_text])
        if next_prefix:
            result += timedelta(days=7)
        return result

    # Month-day forms: "may 23", "23 may", "may 23 2026", "may 23rd".
    month_day = _parse_month_day(s, today)
    if month_day is not None:
        return month_day

    return None


def _parse_month_day(s: str, today: date) -> date | None:
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s)
    tokens = re.split(r"[\s,]+", cleaned.strip())
    month: int | None = None
    day: int | None = None
    year: int | None = None
    for tok in tokens:
        if tok in _MONTHS:
            month = _MONTHS[tok]
        elif tok.isdigit():
            n = int(tok)
            if n > 31:
                year = n
            elif day is None:
                day = n
            else:
                year = n
    if month is None or day is None:
        return None
    if year is None:
        year = today.year
        candidate = _safe_date(year, month, day)
        # A month-day in the past means next year — you don't set a deadline
        # in the past. A 14-day grace keeps a just-slipped deadline ("May 18"
        # said on May 20) in the recent past rather than rolling it a year out.
        if candidate is not None and candidate < today - timedelta(days=14):
            candidate = _safe_date(year + 1, month, day)
        return candidate
    return _safe_date(year, month, day)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def due_status(
    deadline_date: date | None, *, today: date | None = None,
    within_days: int = DUE_SOON_WINDOW_DAYS,
) -> str:
    """Classify a parsed deadline relative to today.

    Returns one of: 'overdue', 'due_today', 'due_soon', 'scheduled', 'unknown'.
    'unknown' is for deadlines that didn't parse — surfaced separately so a
    real deadline isn't silently dropped just because the wording was odd.
    """
    if deadline_date is None:
        return "unknown"
    today = today or date.today()
    if deadline_date < today:
        return "overdue"
    if deadline_date == today:
        return "due_today"
    if deadline_date <= today + timedelta(days=within_days):
        return "due_soon"
    return "scheduled"


def days_until(deadline_date: date, *, today: date | None = None) -> int:
    """Signed day delta — negative when overdue, 0 today, positive when ahead."""
    today = today or date.today()
    return (deadline_date - today).days
