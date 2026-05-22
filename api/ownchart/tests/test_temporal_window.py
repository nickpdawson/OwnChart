"""Temporal-window parser tests (FU-TEMPORAL-WINDOW, 2026-05-22).

PM-fixed semantics:
  - "last week" → trailing 7 days ending today (NOT future)
  - "last calendar week" → previous Monday-Sunday
  - "this week" → current Monday-Sunday (may include forward days)
  - "next week" → next Monday-Sunday (future)
  - "yesterday" / "today" / "tomorrow" → single days
  - "last month" → trailing 30 days
  - "this month" / "this year" → calendar month / year-to-date
  - "the past N days" → trailing N days

All tests freeze ``now`` at 2026-05-22 (UTC, a Friday) per PM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ownchart.retrieval.temporal import (
    TemporalWindow,
    parse_temporal_window,
)


FROZEN_NOW = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)  # Friday


# ---------------------------------------------------------------------------
# "last week" is past-only — the bug Nick caught


def test_last_week_resolves_to_trailing_7_days_past_only():
    """The exact bug: 'last week' was getting future events. Must
    resolve to (now - 7d, now) per PM."""
    w = parse_temporal_window("compare my sleep last week", now=FROZEN_NOW)
    assert w is not None
    assert w.semantics == "trailing_7_days"
    assert w.time_max == FROZEN_NOW
    assert w.time_min == FROZEN_NOW - timedelta(days=7)
    # And future days are NOT in the window.
    next_monday = datetime(2026, 5, 25, tzinfo=timezone.utc)
    assert next_monday > w.time_max


def test_past_week_same_as_last_week():
    w = parse_temporal_window("past week training data", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_7_days"


def test_previous_week_same_as_last_week():
    w = parse_temporal_window("previous week summary", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_7_days"


# ---------------------------------------------------------------------------
# "last calendar week" — explicit Mon-Sun semantics


def test_last_calendar_week_resolves_to_prior_monday_through_sunday():
    """Friday 2026-05-22 → previous calendar week is Mon 2026-05-11
    through Sun 2026-05-17."""
    w = parse_temporal_window(
        "show me last calendar week", now=FROZEN_NOW,
    )
    assert w is not None
    assert w.semantics == "calendar_week_prior"
    assert w.time_min.date() == datetime(2026, 5, 11).date()  # Monday
    assert w.time_max.date() == datetime(2026, 5, 17).date()  # Sunday
    assert w.time_min.hour == 0
    # End-of-week boundary precision (just before midnight Mon).
    assert w.time_max.hour == 23


def test_last_calendar_week_beats_last_week_in_order():
    """Pattern priority: 'last calendar week' must be matched
    before 'last week' since it's a more specific phrase."""
    w = parse_temporal_window(
        "summarize last calendar week training",
        now=FROZEN_NOW,
    )
    assert w is not None
    assert w.semantics == "calendar_week_prior"
    # NOT trailing_7_days
    assert w.semantics != "trailing_7_days"


# ---------------------------------------------------------------------------
# "this week" — current Mon-Sun


def test_this_week_resolves_to_current_calendar_week():
    """Friday 2026-05-22 → this week is Mon 2026-05-18 through
    Sun 2026-05-24 (includes forward days)."""
    w = parse_temporal_window("what is on my calendar this week",
                              now=FROZEN_NOW)
    assert w is not None
    assert w.semantics == "calendar_week_current"
    assert w.time_min.date() == datetime(2026, 5, 18).date()  # Monday
    assert w.time_max.date() == datetime(2026, 5, 24).date()  # Sunday
    # The window DOES include today and forward through Sunday.
    assert w.time_min <= FROZEN_NOW <= w.time_max


# ---------------------------------------------------------------------------
# "next week" — future-only


def test_next_week_resolves_to_future_calendar_week():
    """Friday 2026-05-22 → next week is Mon 2026-05-25 through
    Sun 2026-05-31."""
    w = parse_temporal_window("am I free next week", now=FROZEN_NOW)
    assert w is not None
    assert w.semantics == "calendar_week_next"
    assert w.time_min.date() == datetime(2026, 5, 25).date()
    assert w.time_max.date() == datetime(2026, 5, 31).date()
    # Future-only: time_min must be after today.
    assert w.time_min > FROZEN_NOW


# ---------------------------------------------------------------------------
# Single-day phrases


def test_yesterday():
    w = parse_temporal_window("yesterday's data", now=FROZEN_NOW)
    assert w is not None and w.semantics == "single_day_yesterday"
    assert w.time_min.date() == datetime(2026, 5, 21).date()
    assert w.time_max.date() == datetime(2026, 5, 21).date()


def test_today():
    w = parse_temporal_window("show today only", now=FROZEN_NOW)
    assert w is not None and w.semantics == "single_day_today"
    assert w.time_min.date() == FROZEN_NOW.date()


def test_tomorrow():
    w = parse_temporal_window("what's tomorrow", now=FROZEN_NOW)
    assert w is not None and w.semantics == "single_day_tomorrow"
    assert w.time_min.date() == datetime(2026, 5, 23).date()


# ---------------------------------------------------------------------------
# Trailing-N-days phrases


def test_in_the_last_n_days():
    w = parse_temporal_window("in the last 14 days", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_n_days"
    assert w.time_min == FROZEN_NOW - timedelta(days=14)
    assert w.time_max == FROZEN_NOW


def test_the_past_n_days():
    w = parse_temporal_window("the past 30 days", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_n_days"
    assert w.time_min == FROZEN_NOW - timedelta(days=30)


def test_last_n_days():
    w = parse_temporal_window("last 3 days summary", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_n_days"
    assert w.time_min == FROZEN_NOW - timedelta(days=3)


def test_n_days_too_large_returns_none():
    """Sanity cap at 10 years prevents silly windows."""
    w = parse_temporal_window("the past 99999 days", now=FROZEN_NOW)
    # Falls through to None (no other pattern matches "99999").
    assert w is None


# ---------------------------------------------------------------------------
# Month + year-to-date


def test_last_month():
    w = parse_temporal_window("last month average HR", now=FROZEN_NOW)
    assert w is not None and w.semantics == "trailing_30_days"
    assert w.time_min == FROZEN_NOW - timedelta(days=30)


def test_this_month_starts_at_first_of_month():
    w = parse_temporal_window("this month total steps", now=FROZEN_NOW)
    assert w is not None and w.semantics == "calendar_month_current"
    assert w.time_min == datetime(2026, 5, 1, tzinfo=timezone.utc)
    # Last microsecond of May 31.
    assert w.time_max.date() == datetime(2026, 5, 31).date()


def test_year_to_date():
    w = parse_temporal_window("year to date workouts", now=FROZEN_NOW)
    assert w is not None and w.semantics == "year_to_date"
    assert w.time_min == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert w.time_max == FROZEN_NOW


# ---------------------------------------------------------------------------
# No phrase → None


def test_no_phrase_returns_none():
    assert parse_temporal_window(
        "what medications am I on", now=FROZEN_NOW,
    ) is None
    assert parse_temporal_window(
        "tell me about my eye surgery", now=FROZEN_NOW,
    ) is None
    assert parse_temporal_window("", now=FROZEN_NOW) is None
    assert parse_temporal_window(None, now=FROZEN_NOW) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wearable+temporal: the exact bug Nick reported


def test_compare_sleep_hrv_last_week_resolves_to_trailing_7_days():
    """The literal failing question. Window MUST be past-only."""
    question = (
        "Compare my sleep, HRV, resting HR, and training last week. "
        "Look at my calendar too."
    )
    w = parse_temporal_window(question, now=FROZEN_NOW)
    assert w is not None
    assert w.semantics == "trailing_7_days"
    # Future is excluded.
    next_monday = datetime(2026, 5, 25, tzinfo=timezone.utc)
    assert next_monday > w.time_max
    # Past 7 days is included.
    six_days_ago = FROZEN_NOW - timedelta(days=6)
    assert w.time_min <= six_days_ago <= w.time_max


def test_this_week_calendar_question_includes_future_through_sunday():
    """The other Nick test: 'What's on my calendar this week?'
    SHOULD include the rest of this week's events."""
    w = parse_temporal_window(
        "What is on my calendar this week?", now=FROZEN_NOW,
    )
    assert w is not None
    assert w.semantics == "calendar_week_current"
    # Sun 2026-05-24 falls in the window (forward of Fri).
    sunday = datetime(2026, 5, 24, 12, tzinfo=timezone.utc)
    assert w.time_min <= sunday <= w.time_max
