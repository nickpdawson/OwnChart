"""Demo calendar seed — ~2 years of synthetic events for the demo user.

Builds a single ``ios_eventkit`` ``calendar_source`` row for the demo
user's primary person_record and populates it with weekly recurring
work + workout, monthly recurring, annual life events, scheduled
travel blocks, and sparse one-offs. ~600-900 events over 2 years.

Idempotent: skips silently if any calendar_source already exists for
the demo user. Deterministic given the seed anchor (the day the seed
runs) so re-seeding after a wipe yields identical data.

No PHI. No real names. Generic professional persona. Safe to ship.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, time, timedelta, timezone
from random import Random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit_event import AuditEvent
from ..models.calendar_event import CalendarEvent
from ..models.calendar_source import CalendarSource
from ..models.membership import Membership
from ..models.person_record import PersonRecord
from ..models.user import User
from .config import get_settings
from .logger import get_logger

log = get_logger("ownchart.core.demo_calendar_seed")


DEMO_CALENDAR_EXTERNAL_ID = "demo:avery-primary"
DEMO_CALENDAR_DISPLAY_NAME = "Calendar"
_HISTORY_DAYS = 730  # ~2 years
_NAMESPACE = "ownchart.demo.calendar.v1"


def _ical_uid(seed: str) -> str:
    return hashlib.sha1(f"{_NAMESPACE}|{seed}".encode()).hexdigest()


def _utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Static event templates
#
# Weekly recurring weekday slots. Tuple shape:
#   (weekday, hour, minute, duration_min, title, category)
# weekday: 0=Mon ... 6=Sun.

_WEEKLY: tuple[tuple[int, int, int, int, str, str], ...] = (
    # Work
    (0,  9,  0, 30, "Weekly planning",           "work"),
    (1, 10,  0, 15, "Team standup",              "work"),
    (2, 14,  0, 30, "Cross-functional sync",     "work"),
    (3, 15,  0, 30, "1:1 with manager",          "work"),
    (4, 16,  0, 45, "Friday demo / show & tell", "work"),
    # Personal / fitness
    (0,  6, 30, 45, "Strength session",          "fitness"),
    (2,  6, 30, 45, "Strength session",          "fitness"),
    (4,  6, 30, 45, "Strength session",          "fitness"),
    (5,  9,  0, 60, "Long run",                  "fitness"),
)

# Bi-weekly (every other week, anchored on the Wed of week index even).
#   (weekday, hour, minute, duration_min, title, category, week_parity)
_BIWEEKLY: tuple[tuple[int, int, int, int, str, str, int], ...] = (
    (1, 18, 30, 90, "Book club", "social", 0),
)

# Monthly: (weekday, ordinal-in-month, hour, minute, duration_min, title, category)
# ordinal: 1=first, 2=second, 3=third, 4=fourth, -1=last.
_MONTHLY: tuple[tuple[int, int, int, int, int, str, str], ...] = (
    (0,  1, 11,  0, 60, "Monthly business review", "work"),
    (4,  3, 16,  0, 60, "Town hall",               "work"),
    (2,  2, 17, 30, 60, "Therapy",                 "health"),
)

# Annual fixed-date events: (month, day, all_day, hour, duration_min, title, category)
_ANNUAL: tuple[tuple[int, int, bool, int, int, str, str], ...] = (
    (1,  1, True,  0,    0, "New Year's Day",                    "holiday"),
    (2, 14, False, 19,  90, "Valentine's Day — dinner reserved",  "life"),
    (3, 14, True,  0,    0, "Spouse's birthday",                 "life"),
    (5, 12, True,  0,    0, "Mother's Day",                      "life"),
    (6, 16, True,  0,    0, "Father's Day",                      "life"),
    (6,  3, True,  0,    0, "Kid's birthday — younger",          "life"),
    (7,  4, True,  0,    0, "Independence Day",                  "holiday"),
    (8, 22, True,  0,    0, "Wedding anniversary",               "life"),
    (10, 9, True,  0,    0, "Kid's birthday — older",            "life"),
    (10,31, False, 17, 120, "Halloween — trick-or-treat",        "life"),
    (12,24, True,  0,    0, "Christmas Eve",                     "holiday"),
    (12,25, True,  0,    0, "Christmas Day",                     "holiday"),
    (12,31, False, 20, 240, "New Year's Eve",                    "social"),
)

# Multi-day all-day travel blocks, defined as (days_back_start,
# days_back_end_inclusive, title, category). days_back is from
# the seed anchor (the day the seed runs), so blocks scale with
# whenever the demo is rebuilt.
_TRAVEL_BLOCKS: tuple[tuple[int, int, str, str], ...] = (
    (700, 694, "Family vacation — beach week",            "travel"),
    (610, 605, "Long weekend — visit friends",            "travel"),
    (548, 543, "Thanksgiving travel — visit family",      "travel"),
    (524, 517, "Winter holiday — at home with family",    "holiday"),
    (461, 457, "Ski trip — Park City",                    "travel"),
    (415, 412, "Conference: customer summit (Chicago)",   "work-travel"),
    (390, 386, "Spring break with kids",                  "travel"),
    (363, 359, "Industry conference — keynote week",      "work-travel"),
    (319, 310, "Family vacation — Italy (10 days)",       "travel"),
    (260, 258, "Work offsite — team planning",            "work-travel"),
    (220, 217, "Long weekend — wedding (out of state)",   "life-travel"),
    (180, 176, "Thanksgiving travel",                     "travel"),
    (156, 149, "Winter holiday",                          "holiday"),
    (100,  96, "Ski trip — Tahoe",                        "travel"),
    ( 60,  57, "Conference: spring product week",         "work-travel"),
    ( 45,  41, "Spring break — kids",                     "travel"),
)

# Within work-travel blocks, drop 1-2 nested meetings per day to make
# the trip feel populated. Anchored to start_day + offset_days.
_TRAVEL_NESTED_PATTERNS: tuple[tuple[int, int, int, str], ...] = (
    # (day_offset, hour, duration_min, title)
    (0,  9,  90, "Conference: opening keynote"),
    (0, 19, 120, "Customer dinner"),
    (1,  9, 240, "Conference: morning sessions"),
    (1, 14, 180, "Conference: afternoon track"),
    (2, 10, 120, "Workshop: hands-on"),
    (2, 18,  90, "Speaker reception"),
)

# Routine annual health: (month, day, hour, duration_min, title, category)
_ROUTINE_HEALTH: tuple[tuple[int, int, int, int, str, str], ...] = (
    (1, 18,  9, 60, "Annual physical",          "health"),
    (2, 12, 14, 60, "Dentist cleaning",          "health"),
    (4,  9, 11, 45, "Eye exam",                  "health"),
    (8, 14, 14, 60, "Dentist cleaning",          "health"),
    (10,15, 16, 15, "Flu shot",                  "health"),
)

# Sparser one-off pools. Drawn randomly for each calendar week with
# weighted probability so the calendar feels lived-in but not packed.
_ONE_OFF_POOL: tuple[tuple[str, str, int], ...] = (
    ("Lunch with old colleague",          "social",  60),
    ("Coffee with friend",                "social",  45),
    ("Parent-teacher conference",         "life",    30),
    ("Kid's soccer game",                 "life",    90),
    ("Kid's piano recital",               "life",    60),
    ("Date night",                        "life",   120),
    ("Hair appointment",                  "personal", 60),
    ("Car service",                       "personal", 90),
    ("HVAC service",                      "personal", 60),
    ("Hike with friends",                 "fitness",180),
    ("Book club",                         "social",  90),
    ("Volunteer shift",                   "social", 180),
    ("Birthday party (friend)",           "social", 180),
    ("House guests arriving",             "life",    60),
    ("Quarterly board prep",              "work",   120),
    ("Interview candidate",               "work",    45),
    ("Vendor demo",                       "work",    60),
    ("Doctor follow-up",                  "health",  30),
    ("PT appointment",                    "health",  45),
    ("Yoga class",                        "fitness", 75),
    ("Soccer practice (kids)",            "life",    90),
    ("School pickup early — kid sick",    "life",    60),
)


# ---------------------------------------------------------------------------
# Helpers


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    """Return the date of the nth occurrence of `weekday` in (year, month).

    ordinal: 1..4 from start, -1 for last. Returns None if it falls off
    the end of the month (shouldn't happen for 1-4 weekdays, but kept
    safe).
    """
    if ordinal == -1:
        # Walk back from end of month.
        d = date(year, month, 28) + timedelta(days=4)
        d = d.replace(day=1) - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    d = date(year, month, 1)
    # Skip to first occurrence of weekday.
    d += timedelta(days=(weekday - d.weekday()) % 7)
    d += timedelta(weeks=ordinal - 1)
    if d.month != month:
        return None
    return d


def _iter_dates(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Event generation


def _generate_events(
    source_id: uuid.UUID,
    record_id: uuid.UUID,
    anchor_today: date,
) -> list[CalendarEvent]:
    rng = Random(f"{_NAMESPACE}|{anchor_today.isoformat()}")
    start_date = anchor_today - timedelta(days=_HISTORY_DAYS)
    end_date = anchor_today

    events: list[CalendarEvent] = []
    seen_keys: set[str] = set()

    def _add(
        *,
        start_at: datetime,
        end_at: datetime,
        title: str,
        all_day: bool,
        category: str,
        key: str,
    ):
        if key in seen_keys:
            return
        seen_keys.add(key)
        events.append(CalendarEvent(
            person_record_id=record_id,
            calendar_source_id=source_id,
            external_id=key,
            external_modified_at=start_at,
            start_at=start_at,
            end_at=end_at,
            all_day=all_day,
            title=title,
            location=None,
            notes=None,
            attendees_count=None,
            privacy_mode_applied="full_details",
            tombstoned_at=None,
            raw_metadata={
                "demo_seed": True,
                "category": category,
                "calendar": {"ical_uid": _ical_uid(key)},
            },
        ))

    # Pre-compute travel block date ranges so we can skip recurring
    # work events that fall inside them.
    travel_ranges: list[tuple[date, date, str, str]] = []
    for days_back_start, days_back_end, title, category in _TRAVEL_BLOCKS:
        block_start = anchor_today - timedelta(days=days_back_start)
        block_end = anchor_today - timedelta(days=days_back_end)
        if block_end < start_date or block_start > end_date:
            continue
        # Clamp to window.
        clipped_start = max(block_start, start_date)
        clipped_end = min(block_end, end_date)
        travel_ranges.append((clipped_start, clipped_end, title, category))

    def _in_travel(d: date) -> bool:
        return any(s <= d <= e for s, e, _, _ in travel_ranges)

    # 1. Weekly recurring.
    for d in _iter_dates(start_date, end_date):
        if _in_travel(d):
            continue
        for wd, hour, minute, dur, title, category in _WEEKLY:
            if d.weekday() != wd:
                continue
            # Skip Saturday long run on 30% of weeks (life happens).
            if category == "fitness" and wd == 5 and rng.random() < 0.3:
                continue
            start = _utc(d, time(hour, minute))
            end = start + timedelta(minutes=dur)
            key = f"weekly|{d.isoformat()}|{wd}|{hour:02d}{minute:02d}|{title}"
            _add(start_at=start, end_at=end, title=title, all_day=False,
                 category=category, key=key)

    # 2. Bi-weekly.
    for d in _iter_dates(start_date, end_date):
        if _in_travel(d):
            continue
        # Anchor parity off ISO week number to keep deterministic.
        iso_week = d.isocalendar()[1]
        for wd, hour, minute, dur, title, category, parity in _BIWEEKLY:
            if d.weekday() != wd or (iso_week % 2) != parity:
                continue
            start = _utc(d, time(hour, minute))
            end = start + timedelta(minutes=dur)
            key = f"biweekly|{d.isoformat()}|{title}"
            _add(start_at=start, end_at=end, title=title, all_day=False,
                 category=category, key=key)

    # 3. Monthly.
    months_seen: set[tuple[int, int]] = set()
    for d in _iter_dates(start_date, end_date):
        months_seen.add((d.year, d.month))
    for year, month in sorted(months_seen):
        for wd, ordinal, hour, minute, dur, title, category in _MONTHLY:
            mday = _nth_weekday_of_month(year, month, wd, ordinal)
            if mday is None or mday < start_date or mday > end_date:
                continue
            if _in_travel(mday):
                continue
            start = _utc(mday, time(hour, minute))
            end = start + timedelta(minutes=dur)
            key = f"monthly|{mday.isoformat()}|{title}"
            _add(start_at=start, end_at=end, title=title, all_day=False,
                 category=category, key=key)

    # 4. Annual fixed-date.
    years_seen = sorted({d.year for d in _iter_dates(start_date, end_date)})
    for year in years_seen:
        for month, day, all_day, hour, dur, title, category in _ANNUAL:
            try:
                dd = date(year, month, day)
            except ValueError:
                continue
            if dd < start_date or dd > end_date:
                continue
            if all_day:
                start = _utc(dd, time(0, 0))
                end = _utc(dd + timedelta(days=1), time(0, 0))
            else:
                start = _utc(dd, time(hour, 0))
                end = start + timedelta(minutes=dur)
            key = f"annual|{dd.isoformat()}|{title}"
            _add(start_at=start, end_at=end, title=title, all_day=all_day,
                 category=category, key=key)

    # 5. Routine health.
    for year in years_seen:
        for month, day, hour, dur, title, category in _ROUTINE_HEALTH:
            try:
                dd = date(year, month, day)
            except ValueError:
                continue
            if dd < start_date or dd > end_date:
                continue
            start = _utc(dd, time(hour, 0))
            end = start + timedelta(minutes=dur)
            key = f"health|{dd.isoformat()}|{title}"
            _add(start_at=start, end_at=end, title=title, all_day=False,
                 category=category, key=key)

    # 6. Travel blocks (one all-day per day in the block) + nested
    # meetings for work-travel categories.
    for block_start, block_end, title, category in travel_ranges:
        days = list(_iter_dates(block_start, block_end))
        for i, d in enumerate(days):
            start = _utc(d, time(0, 0))
            end = _utc(d + timedelta(days=1), time(0, 0))
            label = title if len(days) == 1 else f"{title} (day {i+1}/{len(days)})"
            key = f"travel|{d.isoformat()}|{title}"
            _add(start_at=start, end_at=end, title=label, all_day=True,
                 category=category, key=key)
        if "work" in category:
            for offset, hour, dur, sub_title in _TRAVEL_NESTED_PATTERNS:
                if offset >= len(days):
                    continue
                d = days[offset]
                start = _utc(d, time(hour, 0))
                end = start + timedelta(minutes=dur)
                key = f"travel-nested|{d.isoformat()}|{sub_title}"
                _add(start_at=start, end_at=end, title=sub_title,
                     all_day=False, category="work-travel", key=key)

    # 7. Sparse one-offs — ~2 per week on average, weekend-heavy for
    # social/life, weekday-heavy for work/health.
    for d in _iter_dates(start_date, end_date):
        if _in_travel(d):
            continue
        # ~2-3 one-offs per week → ~0.35 prob per day.
        if rng.random() > 0.35:
            continue
        pool = list(_ONE_OFF_POOL)
        rng.shuffle(pool)
        title, category, dur = pool[0]
        # Steer category by weekday plausibility.
        is_weekend = d.weekday() >= 5
        for cand_title, cand_cat, cand_dur in pool:
            if is_weekend and cand_cat in ("social", "life", "fitness", "personal"):
                title, category, dur = cand_title, cand_cat, cand_dur
                break
            if not is_weekend and cand_cat in ("work", "health", "personal"):
                title, category, dur = cand_title, cand_cat, cand_dur
                break
        # Time of day by category.
        if category in ("work", "health"):
            hour = rng.choice([9, 10, 11, 13, 14, 15])
        elif category == "fitness":
            hour = rng.choice([6, 7, 17, 18])
        else:
            hour = rng.choice([12, 18, 19, 20])
        minute = rng.choice([0, 15, 30])
        start = _utc(d, time(hour, minute))
        end = start + timedelta(minutes=dur)
        key = f"oneoff|{d.isoformat()}|{title}|{hour:02d}{minute:02d}"
        _add(start_at=start, end_at=end, title=title, all_day=False,
             category=category, key=key)

    return events


# ---------------------------------------------------------------------------
# Seed entrypoint


async def _ensure_demo_record(db: AsyncSession, user: User) -> uuid.UUID | None:
    """Find or create the demo user's self person_record.

    Mirrors auth.py:_bootstrap_self_record so the demo user matches
    the shape of a normal first-user signup. Idempotent.
    """
    membership = (await db.execute(
        select(Membership).where(Membership.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    if membership is not None:
        return membership.person_record_id

    now = datetime.now(timezone.utc)
    record = PersonRecord(
        display_name="Me",
        is_self=True,
        created_by_user_id=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.flush()
    db.add(Membership(
        user_id=user.id,
        person_record_id=record.id,
        role="owner",
        accepted_at=now,
        created_at=now,
    ))
    user.default_person_record_id = record.id
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=record.id,
        event_type="membership_created",
        subject_type="membership",
        subject_id=None,
        detail={"role": "owner", "granted_via": "demo_calendar_seed"},
    ))
    await db.flush()
    log.info("demo_self_record_bootstrapped", record_id=str(record.id))
    return record.id


async def seed_demo_calendar_if_needed(db: AsyncSession) -> int:
    """Seed ~2 years of synthetic calendar events for the demo user.

    Returns the number of CalendarEvent rows created (0 if skipped).
    Skips silently when demo mode is off, the demo user is missing,
    or a calendar_source already exists for that user.
    """
    s = get_settings()
    if not s.demo_mode:
        return 0

    demo_user = (await db.execute(
        select(User).where(User.email == s.demo_user_email)
    )).scalar_one_or_none()
    if demo_user is None:
        log.info("demo_calendar_seed_skip_no_user")
        return 0

    record_id = await _ensure_demo_record(db, demo_user)
    if record_id is None:
        log.info("demo_calendar_seed_skip_no_record")
        return 0

    existing = (await db.execute(
        select(CalendarSource.id)
        .where(CalendarSource.user_id == demo_user.id)
        .where(CalendarSource.person_record_id == record_id)
        .limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return 0

    now = datetime.now(timezone.utc)
    source = CalendarSource(
        person_record_id=record_id,
        user_id=demo_user.id,
        adapter_type="ios_eventkit",
        external_id=DEMO_CALENDAR_EXTERNAL_ID,
        display_name=DEMO_CALENDAR_DISPLAY_NAME,
        privacy_mode="full_details",
        llm_full_details_consent=True,
        connected_at=now,
        last_sync_at=now,
        last_sync_status="ok",
    )
    db.add(source)
    await db.flush()

    events = _generate_events(
        source_id=source.id,
        record_id=record_id,
        anchor_today=now.date(),
    )
    for ev in events:
        db.add(ev)
    await db.commit()
    log.info("demo_calendar_seeded",
             source_id=str(source.id),
             record_id=str(record_id),
             events=len(events),
             window_days=_HISTORY_DAYS)
    return len(events)
