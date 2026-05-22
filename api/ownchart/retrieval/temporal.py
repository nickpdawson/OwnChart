"""Temporal-phrase parser for natural-language retrieval windows.

Beta 1 FU-TEMPORAL-WINDOW (2026-05-22): the conversations + Ask
paths default to a forward-leaning window (now to now+30d) on the
calendar fetch. When the user says "last week" the model would
otherwise see future events in the same context — leading to the
exact bug Nick caught (a "last week" question answered with travel
events scheduled for the WEEK AHEAD).

This module returns ``(time_min, time_max) | None`` for the
phrases a conversational health query typically uses. PM-chosen
semantics:

  - "last week" / "past week" / "previous week" / "the past 7 days"
        → trailing 7 days ending **today** (now - 7d, now). PM
          preference for conversational health queries: ride the
          rolling 7-day window, not a calendar week, so the user
          doesn't get a different answer Mon vs Sun.

  - "last calendar week"
        → previous Monday 00:00 → Sunday 23:59:59 in the caller's
          tz. Use when the user EXPLICITLY says "calendar week"; the
          retrieval surface should not infer this.

  - "this week"
        → current Monday 00:00 → Sunday 23:59:59. May include
          forward-of-now days through the end of this week.

  - "next week"
        → next Monday 00:00 → Sunday 23:59:59. Future-only.

  - "yesterday" → previous day 00:00 → previous day 23:59:59.
  - "today"     → today 00:00 → today 23:59:59.
  - "tomorrow"  → tomorrow 00:00 → tomorrow 23:59:59.

  - "last month" → trailing 30 days ending today.
  - "this month" → first → last day of the current month.
  - "this year" / "year to date" → Jan 1 → today.

  - "in the last N days" (N = integer) → trailing N days ending today.

If no temporal phrase is detected, returns None and the caller
keeps its default window.

Pure-function. The ``now`` parameter is always-injectable so tests
can freeze the date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone


@dataclass(frozen=True)
class TemporalWindow:
    """Resolved (time_min, time_max) plus the phrase that matched —
    useful for telemetry counts and downstream prompt framing."""

    time_min: datetime
    time_max: datetime
    phrase: str
    semantics: str  # "trailing_n_days" | "calendar_week" | "single_day" |
                    # "calendar_month" | "year_to_date"


# Ordered patterns — first match wins. Multi-word phrases must come
# before their single-word substrings so "last calendar week" beats
# "last week" / "calendar week".
#
# Each entry: (compiled_regex, semantics_label).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\blast calendar week\b"), "calendar_week_prior"),
    (re.compile(r"\bthis calendar week\b"), "calendar_week_current"),
    (re.compile(r"\bnext calendar week\b"), "calendar_week_next"),
    (re.compile(r"\bthe (?:past|last) (\d+) days?\b"), "trailing_n_days"),
    (re.compile(r"\bin the (?:past|last) (\d+) days?\b"), "trailing_n_days"),
    (re.compile(r"\blast (\d+) days?\b"), "trailing_n_days"),
    (re.compile(r"\bpast (\d+) days?\b"), "trailing_n_days"),
    (re.compile(r"\blast week\b"), "trailing_7_days"),
    (re.compile(r"\bpast week\b"), "trailing_7_days"),
    (re.compile(r"\bprevious week\b"), "trailing_7_days"),
    (re.compile(r"\bthis week\b"), "calendar_week_current"),
    (re.compile(r"\bnext week\b"), "calendar_week_next"),
    (re.compile(r"\byesterday\b"), "single_day_yesterday"),
    (re.compile(r"\btoday\b"), "single_day_today"),
    (re.compile(r"\btomorrow\b"), "single_day_tomorrow"),
    (re.compile(r"\blast month\b"), "trailing_30_days"),
    (re.compile(r"\bpast month\b"), "trailing_30_days"),
    (re.compile(r"\bthis month\b"), "calendar_month_current"),
    (re.compile(r"\b(?:this year|year to date|ytd)\b"), "year_to_date"),
]


def parse_temporal_window(
    question: str,
    *,
    now: datetime | None = None,
) -> TemporalWindow | None:
    """Detect a relative-date phrase in ``question`` and resolve to
    a concrete UTC time window.

    Returns ``None`` when no phrase matches; the caller keeps its
    default behavior in that case.

    ``now`` is always-injectable so tests freeze it (PM directive
    for "frozen current date 2026-05-22" tests). When None, uses
    ``datetime.now(timezone.utc)``.
    """
    if not question or not isinstance(question, str):
        return None
    q = question.lower()
    now = now or datetime.now(timezone.utc)
    # Strip timezone awareness off for date-math; reattach UTC at end.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    for pat, semantics in _PATTERNS:
        m = pat.search(q)
        if not m:
            continue
        phrase = m.group(0)
        # Each branch returns the (time_min, time_max, semantics) tuple
        # with the matching phrase preserved for telemetry.
        if semantics == "trailing_7_days":
            return TemporalWindow(
                time_min=now - timedelta(days=7),
                time_max=now,
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "trailing_30_days":
            return TemporalWindow(
                time_min=now - timedelta(days=30),
                time_max=now,
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "trailing_n_days":
            try:
                n = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if n <= 0 or n > 3650:  # sanity cap at 10y
                continue
            return TemporalWindow(
                time_min=now - timedelta(days=n),
                time_max=now,
                phrase=phrase,
                semantics="trailing_n_days",
            )
        if semantics == "calendar_week_prior":
            wm = _monday_of(now) - timedelta(days=7)
            return TemporalWindow(
                time_min=wm,
                time_max=wm + timedelta(days=7) - timedelta(microseconds=1),
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "calendar_week_current":
            wm = _monday_of(now)
            return TemporalWindow(
                time_min=wm,
                time_max=wm + timedelta(days=7) - timedelta(microseconds=1),
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "calendar_week_next":
            wm = _monday_of(now) + timedelta(days=7)
            return TemporalWindow(
                time_min=wm,
                time_max=wm + timedelta(days=7) - timedelta(microseconds=1),
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "single_day_yesterday":
            day = (now - timedelta(days=1)).date()
            return _single_day(day, phrase, semantics)
        if semantics == "single_day_today":
            return _single_day(now.date(), phrase, semantics)
        if semantics == "single_day_tomorrow":
            day = (now + timedelta(days=1)).date()
            return _single_day(day, phrase, semantics)
        if semantics == "calendar_month_current":
            start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )
            # End = first of next month - 1us.
            if start.month == 12:
                next_first = start.replace(year=start.year + 1, month=1)
            else:
                next_first = start.replace(month=start.month + 1)
            return TemporalWindow(
                time_min=start,
                time_max=next_first - timedelta(microseconds=1),
                phrase=phrase,
                semantics=semantics,
            )
        if semantics == "year_to_date":
            start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0,
            )
            return TemporalWindow(
                time_min=start,
                time_max=now,
                phrase=phrase,
                semantics=semantics,
            )

    return None


def _monday_of(dt: datetime) -> datetime:
    """Return Monday 00:00 UTC of the week containing ``dt``."""
    # weekday(): Monday=0, Sunday=6
    monday_date = dt.date() - timedelta(days=dt.weekday())
    return datetime.combine(monday_date, time(0, 0), tzinfo=timezone.utc)


def _single_day(d, phrase: str, semantics: str) -> TemporalWindow:
    start = datetime.combine(d, time(0, 0), tzinfo=timezone.utc)
    end = datetime.combine(d, time(23, 59, 59, 999_999), tzinfo=timezone.utc)
    return TemporalWindow(
        time_min=start, time_max=end, phrase=phrase, semantics=semantics,
    )
