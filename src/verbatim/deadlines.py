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

# A deadline that is ENTIRELY filler. Every one of these means "before the end of today" and none
# of them carries a date, so they resolve to today rather than being stripped down to nothing.
_MEANS_TODAY = frozenset({
    "today", "tonight", "this evening",
    "eod", "end of day", "end of the day", "by end of day", "by the end of the day", "by eod",
    "cob", "close of business", "by cob", "by close of business",
    "end of business", "end of the business day",
})

# Spelled-out day counts. One to ten only -- see the comment at the use site.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1, "a couple of": 2, "a couple": 2, "a few": 3,
}

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

    # WHOLE-STRING FILLER MEANS TODAY, and this has to be checked BEFORE the stripping loop below.
    #
    # `_STRIP_PREFIXES` exists for filler that PRECEDES a date -- "by end of day Friday" -> "Friday".
    # Each entry is matched as `prefix + " "`, so an entry that is the ENTIRE deadline never matches
    # it: "by end of day" fell through to the shorter "by end of", which left the bare word "day",
    # which parses as nothing. So "EOD" resolved to today and "by end of day" did not.
    #
    # Found by feeding the parser the phrases a real transcript produced rather than by reading it.
    if s in _MEANS_TODAY:
        return today

    # Strip leading filler words/phrases (longest first so "by eod" wins over "by").
    changed = True
    while changed:
        changed = False
        for prefix in _STRIP_PREFIXES:
            if s.startswith(prefix + " "):
                s = s[len(prefix) + 1:].strip()
                changed = True
        s = s.strip(" ,")

    # "end of day" spelled out, which was MISSING while "eod" worked -- an inconsistency found by
    # feeding the parser the phrases a real transcript actually produced. The first live extraction
    # of a standup returned "end of day" verbatim, and it came back unparsed while the abbreviation
    # of the same words resolved fine.
    if s in ("today", "tonight", "this evening", "eod", "end of day", "end of the day"):
        return today
    # A time of day on a date is still that date. "tomorrow morning" is tomorrow; the hour is not
    # something a deadline tracker working in whole days can or should represent.
    if s in ("tomorrow", "tmrw", "tom") or re.fullmatch(
        r"tomorrow\s+(morning|afternoon|evening|night)", s
    ):
        return today + timedelta(days=1)
    if s in ("next week", "end of next week"):
        return today + timedelta(days=7)
    if s in ("this week", "end of week", "end of the week", "eow"):
        return _next_weekday(today, 4)  # Friday of the current week-ish
    # End of month: the LAST day of the current month, found by stepping to the first of the next
    # month and back one day -- which handles February and leap years without a table.
    if s in ("end of month", "end of the month", "eom", "month end"):
        first_next = (
            date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
        )
        return first_next - timedelta(days=1)

    # "in 3 days" / "3 days" / "in a week"
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s+days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))
    # And the same in words, because people SAY "in two days" and write "in 2 days". A transcript is
    # speech, so the spelled-out form is the common one here rather than the exotic one. Capped at
    # ten deliberately: past that, counting days is not how anyone expresses a deadline, and a long
    # tail of number words is a parser pretending to understand more than it does.
    m = re.fullmatch(rf"(?:in\s+)?({'|'.join(_NUMBER_WORDS)})\s+days?", s)
    if m:
        return today + timedelta(days=_NUMBER_WORDS[m.group(1)])
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
