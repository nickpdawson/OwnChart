"""Episode Intelligence retrieval planner — the deterministic engine
behind "Make sense of this event" (docs/10 + Nick 2026-05-11 PM).

Pattern: given an anchor (an explicit fact_id, a natural-language
reference like "the surgery about a week ago", or an episode_id), the
planner gathers everything OwnChart can deterministically know about
that life moment + the standardized recovery windows around it, and
returns a structured payload the LLM can synthesize from.

Output shape (consumed by `prompts/episode_intelligence.v1.yaml`):

  {
    "anchor": {
      "fact_id": ..., "label": ..., "date_start": ...,
      "match_confidence": "high|medium|low",
      "match_explanation": "..."
    },
    "what_happened": { facts: [...], sources: [...] },
    "what_they_did": { procedure_facts: [...], translations: [...] },
    "anesthesia_meds": { facts: [...], missing_anesthesia_record: bool },
    "travel_and_life": { events: [...] },
    "body_response": {
      "windows": [
        {"name": "30d_baseline", "from": ..., "to": ..., "metrics": {...}},
        {"name": "7d_before",    ...},
        {"name": "day_of",       ...},
        {"name": "7d_after",     ...},
        {"name": "14d_after",    ...}
      ]
    },
    "follow_up_questions": [...]   # seeded by the planner; LLM may add
  }

V1 deliberately keeps the planner deterministic and the LLM purely
synthesis. The metrics aggregation is best-effort against current
HealthKit / Auto Export data shapes; missing data is reported, not
guessed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument

log = get_logger("ownchart.canonical.episode_intelligence")


# Recovery-window profiles, keyed by episode kind (Q-A3).
# Each tuple is (name, days_offset_from_anchor, days_offset_to_anchor).
# Negative offsets are before the anchor; 0 is the anchor day.
#
# These are internal product knobs — NOT exposed as user settings in
# V1. Episode kind is inferred from the anchor + Episode.kind when
# the candidate is promoted; until then surgery/procedure anchors get
# the surgical-recovery profile.
_RECOVERY_PROFILES: dict[str, tuple[tuple[str, int, int], ...]] = {
    "surgery": (
        ("30d_baseline", -30, -8),
        ("7d_before",    -7,  -1),
        ("day_of",       0,   0),
        ("7d_after",     1,   7),
        ("14d_after",    8,   14),
    ),
    "illness": (
        ("14d_baseline", -14, -4),
        ("3d_before",    -3,  -1),
        ("symptom_day",   0,   0),
        ("acute_7d",      1,   7),
        ("recovery_14d",  8,   14),
    ),
    "injury": (
        ("30d_baseline", -30, -8),
        ("week_of",      -3,   3),
        ("acute_7d",      4,  10),
        ("rehab_30d",    11,  30),
    ),
    "mental_health_episode": (
        ("60d_baseline", -60, -15),
        ("2w_before",    -14, -1),
        ("event_day",     0,   0),
        ("2w_after",      1,  14),
        ("4w_after",     15,  28),
    ),
    "medication_course": (
        ("30d_before",   -30,  -1),
        ("week_1",        0,   6),
        ("week_4",        7,  27),
        ("week_8",       28,  55),
    ),
    "rehab": (
        ("baseline",     -14, -1),
        ("week_1",        0,   6),
        ("week_2",        7,  13),
        ("week_4",       14,  27),
    ),
    "diagnostic_workup": (
        ("month_before", -30, -1),
        ("workup_window", 0,   7),
        ("post_workup_30d", 8, 37),
    ),
    "default": (
        ("30d_baseline", -30, -8),
        ("7d_before",    -7,  -1),
        ("day_of",       0,   0),
        ("7d_after",     1,   7),
        ("14d_after",    8,   14),
    ),
}


def _profile_for_kind(kind: str | None) -> tuple[tuple[str, int, int], ...]:
    if not kind:
        return _RECOVERY_PROFILES["default"]
    return _RECOVERY_PROFILES.get(kind, _RECOVERY_PROFILES["default"])


# Metrics we report on if the user's record has them. The labels come
# from ingest/auto_export.py + ingest/healthkit.py. We do simple
# substring matches because labels carry units etc.
_WEARABLE_METRICS = {
    # Each metric: natural-language needles + HK identifier substrings.
    # HK ingest creates fact labels like
    # `HKQuantityTypeIdentifierHeartRateVariabilitySDNN: 28.48 ms`
    # (all-one-word), and Auto Export produces friendlier labels like
    # `Heart rate variability: 38 ms`. Both shapes need to classify.
    # Bug caught 2026-05-13 PM in golden-path walk: body_response
    # section returned "no aggregated metrics" for every Episode
    # Intelligence run because the classifier only matched
    # natural-phrase shapes that don't exist in native HK labels.
    "rhr": ("resting heart rate", "rhr", "restingheartrate"),
    "hrv": ("heart rate variability", "hrv", "heartratevariability"),
    "heart_rate": ("heart rate", "heartrate"),
    "sleep_duration": (
        "sleep", "time asleep", "sleep duration", "sleepanalysis",
    ),
    "vo2max": ("vo2max", "vo2 max"),
    "spo2": ("oxygen saturation", "spo2", "oxygensaturation"),
}

# Cumulative endurance metrics — sum across the window AND roll up
# to per-day totals + active-day counts. NEVER report these as a
# per-sample mean: HK auto-export emits one fact per ~30s interval
# and "active energy averaged 0.82 kcal/sample" is meaningless to
# the user. Caught 2026-05-13 PM in Nick's endurance review.
_CUMULATIVE_METRICS = {
    "active_energy_kcal": (
        "active energy", "active calories", "activeenergyburned",
    ),
    "exercise_minutes": (
        "exercise time", "appleexercisetime", "apple exercise time",
    ),
    "walking_running_mi": (
        "walking + running", "distancewalkingrunning",
    ),
    "steps_total": ("steps", "stepcount"),
}

# Workout sessions — counted, not summed/meaned. HK emits one fact
# per workout with the label "HKWorkoutType: <duration_seconds>".
_WORKOUT_LABEL_NEEDLES = ("hkworkouttype",)


# Anesthesia proper — anesthetics, paralytics, local anesthetics.
# Conservative; false positives ("aspirin") are worse than false
# negatives.
_ANESTHESIA_KEYWORDS = (
    "anesthesia", "anesthetic", "propofol", "midazolam", "fentanyl",
    "ketamine", "sevoflurane", "isoflurane", "desflurane",
    "rocuronium", "succinylcholine", "lidocaine", "bupivacaine",
    "remifentanil", "sufentanil", "dexmedetomidine", "nitrous oxide",
    "etomidate",
)

# Perioperative support meds (Q-A2): anti-emetics, multimodal analgesia,
# corticosteroids, antibiotics used at induction. These belong in the
# Episode Intelligence narrative but should NOT be conflated with
# the anesthetic agents themselves.
_PERIOPERATIVE_SUPPORT_KEYWORDS = (
    "ondansetron", "zofran", "scopolamine",
    "acetaminophen", "tylenol", "paracetamol",
    "ibuprofen", "ketorolac", "toradol",
    "dexamethasone", "decadron",
    "cefazolin", "ancef",
    "famotidine", "pepcid",
    "metoclopramide", "reglan",
    "ephedrine", "phenylephrine", "neostigmine", "glycopyrrolate",
    "sugammadex", "bridion",
)

_TRAVEL_LIFE_KEYWORDS = (
    "flight", "flown", "travel", "trip", "hotel", "airport",
    "vacation", "wedding", "conference",
)


# ---------------------------------------------------------------------------
# Anchor resolution


_REL_DATE_RE = re.compile(
    r"about\s+(?:a|one)\s+(week|month|year)\s+ago"
    r"|(\d+)\s+(day|week|month|year)s?\s+ago"
    r"|last\s+(week|month|year)",
    re.IGNORECASE,
)

# Explicit calendar-date patterns. Caught during golden-path walk
# 2026-05-13 PM: Nick asked about "eye surgery on may 1 2026" and the
# anchor resolver fell back to "most recent major procedure" (low
# confidence) because the regex above only handled relative dates.
# Three formats supported (case-insensitive on month names):
#   "May 1 2026", "May 1, 2026"     → groups: month_name, day, year?
#   "2026-05-01" (ISO)              → groups: year, month_num, day
#   "5/1/2026", "5/1/26"            → groups: month_num, day, year_short_or_full
_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}
_ABS_DATE_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+(\d{1,2})(?:[,\s]+(\d{4}))?"
    r"|\b(\d{4})-(\d{1,2})-(\d{1,2})\b"
    r"|\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b",
    re.IGNORECASE,
)


def _parse_absolute_date(natural_language: str, *, now: datetime) -> datetime | None:
    """Match an explicit calendar date (May 1 2026 / 2026-05-01 / 5/1/2026)
    and return it as UTC midnight. Returns None if none found. When the
    year is omitted ("May 1"), defaults to the current year — caller can
    still build a ±N-day window around it. When the year is 2-digit
    ("5/1/26"), expands as 2000+yy."""
    m = _ABS_DATE_RE.search(natural_language or "")
    if not m:
        return None
    try:
        if m.group(1):  # "May 1 [2026]"
            month = _MONTH_NAMES[m.group(1).lower()]
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
        elif m.group(4):  # "2026-05-01"
            year = int(m.group(4))
            month = int(m.group(5))
            day = int(m.group(6))
        else:  # "5/1/2026" or "5/1/26"
            month = int(m.group(7))
            day = int(m.group(8))
            year = int(m.group(9))
            if year < 100:
                year += 2000
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None


def _parse_relative_window(natural_language: str, *, now: datetime) -> tuple[datetime, datetime] | None:
    """If the question contains a relative date reference OR an explicit
    calendar date, return a (start, end) UTC window. Otherwise None."""
    abs_date = _parse_absolute_date(natural_language, now=now)
    if abs_date is not None:
        # Tight ±3-day window — explicit dates are intent-confident, so
        # we don't need to fish across weeks.
        return (abs_date - timedelta(days=3), abs_date + timedelta(days=3))
    m = _REL_DATE_RE.search(natural_language or "")
    if not m:
        return None
    if m.group(1):  # "about a week ago"
        unit = m.group(1).lower()
        n = 1
    elif m.group(2):  # "10 days ago"
        n = int(m.group(2))
        unit = m.group(3).lower()
    else:  # "last week"
        unit = m.group(4).lower()
        n = 1
    days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(unit, 7) * n
    # Anchor on calendar midnight, not on `now.time()`. Original code
    # used `now - timedelta(days=N)` directly, so "10 days ago" at
    # 21:30 UTC produced a window starting 21:30 ten days prior —
    # which missed any event at 09:00 / 13:00 on the target day.
    # Caught 2026-05-13 PM: the May-1 13:25 surgery fell 8 hours
    # outside the window for "about 10 days ago" asked at 21:38.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    target = midnight - timedelta(days=days)
    half = max(3, days // 4)  # at least ±3 days for short windows
    half = min(half, 21)       # cap at ±3 weeks for "a year ago"
    start = target - timedelta(days=half)
    end = target + timedelta(days=half + 1) - timedelta(microseconds=1)
    return start, end


async def resolve_anchor(
    db: AsyncSession,
    *,
    fact_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
    natural_language: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Find the best anchor fact for the request.

    Resolution order:
      1. Explicit fact_id (always wins; confidence='high').
      2. Explicit episode_id → episode.primary_fact_id.
      3. NL phrase like "about a week ago" → window-restricted search
         for the highest-significance procedure/event in that window.
      4. NL phrase fallback → just the most recent major_procedure.
    """
    now = now or datetime.now(timezone.utc)

    if fact_id is not None:
        f = await db.get(ExtractedFact, fact_id)
        if f is None:
            return None
        return {
            "fact_id": str(f.id),
            "label": f.display_label or f.label,
            "date_start": f.date_start.isoformat() if f.date_start else None,
            "significance": f.significance,
            "match_confidence": "high",
            "match_explanation": "Explicit fact_id supplied by the caller.",
        }

    if episode_id is not None:
        from ..models.episode import Episode
        ep = await db.get(Episode, episode_id)
        if ep is None or ep.primary_fact_id is None:
            return None
        return await resolve_anchor(db, fact_id=ep.primary_fact_id, now=now)

    if natural_language:
        window = _parse_relative_window(natural_language, now=now)
        if window is not None:
            start, end = window
            rows = list((await db.execute(
                select(ExtractedFact)
                .where(ExtractedFact.date_start.isnot(None))
                .where(ExtractedFact.date_start >= start)
                .where(ExtractedFact.date_start <= end)
                .where(ExtractedFact.significance.in_(
                    ("major_event", "major_procedure")
                ))
                .where(ExtractedFact.review_state.notin_(
                    ("deferred", "rejected", "source_only")
                ))
                .order_by(ExtractedFact.date_start.desc())
                .limit(5)
            )).scalars().all())
            if rows:
                f = rows[0]
                return {
                    "fact_id": str(f.id),
                    "label": f.display_label or f.label,
                    "date_start": f.date_start.isoformat(),
                    "significance": f.significance,
                    "match_confidence": "high" if len(rows) == 1 else "medium",
                    "match_explanation": (
                        f"Found {len(rows)} major event(s) between "
                        f"{start.date().isoformat()} and "
                        f"{end.date().isoformat()}. "
                        f"Choosing the most recent."
                    ),
                }
        # Pure fallback: most recent major procedure.
        f = (await db.execute(
            select(ExtractedFact)
            .where(ExtractedFact.date_start.isnot(None))
            .where(ExtractedFact.significance == "major_procedure")
            .where(ExtractedFact.review_state.notin_(
                ("deferred", "rejected", "source_only")
            ))
            .order_by(ExtractedFact.date_start.desc())
            .limit(1)
        )).scalar_one_or_none()
        if f is not None:
            return {
                "fact_id": str(f.id),
                "label": f.display_label or f.label,
                "date_start": f.date_start.isoformat(),
                "significance": f.significance,
                "match_confidence": "low",
                "match_explanation": (
                    "No relative-date phrase matched; fell back to the "
                    "most recent major_procedure on the record."
                ),
            }
    return None


# ---------------------------------------------------------------------------
# Surrounding-window gathering


def _is_anesthesia(f: ExtractedFact) -> bool:
    s = (f.label or "").lower() + " " + (f.description or "").lower()
    return any(k in s for k in _ANESTHESIA_KEYWORDS)


def _is_perioperative_support(f: ExtractedFact) -> bool:
    s = (f.label or "").lower() + " " + (f.description or "").lower()
    return any(k in s for k in _PERIOPERATIVE_SUPPORT_KEYWORDS)


def _is_travel_or_life(f: ExtractedFact) -> bool:
    s = (f.label or "").lower() + " " + (f.description or "").lower()
    return f.fact_type == "life_context_event" or any(
        k in s for k in _TRAVEL_LIFE_KEYWORDS
    )


def _classify_wearable(label: str) -> str | None:
    s = (label or "").lower()
    for key, needles in _WEARABLE_METRICS.items():
        for n in needles:
            if n in s:
                return key
    return None


_WEARABLE_METHODS = ("health_auto_export", "native_healthkit")


async def _facts_in_window(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    wearable_only: bool = False,
    clinical_only: bool = False,
    limit: int = 2000,
) -> list[ExtractedFact]:
    """Pull facts in [start, end].

    `wearable_only` filters to HK / auto-export observations only.
    `clinical_only` excludes HK / auto-export facts so the same-day
    clinical pull (procedure/encounter/condition/medication) doesn't
    get drowned out by tens of thousands of wearable observations
    that sort earlier by `date_start.asc()` and starve the limit.
    Caught 2026-05-13 PM — limit=200 on a wearable-heavy day clipped
    procedures off entirely.
    """
    stmt = (
        select(ExtractedFact)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= start)
        .where(ExtractedFact.date_start <= end)
        .order_by(ExtractedFact.date_start.asc())
        .limit(limit)
    )
    if wearable_only:
        stmt = stmt.where(ExtractedFact.extraction_method.in_(_WEARABLE_METHODS))
    elif clinical_only:
        stmt = stmt.where(
            ExtractedFact.extraction_method.notin_(_WEARABLE_METHODS)
        )
    return list((await db.execute(stmt)).scalars().all())


def _classify_cumulative(label: str) -> str | None:
    s = (label or "").lower()
    for key, needles in _CUMULATIVE_METRICS.items():
        for n in needles:
            if n in s:
                return key
    return None


def _is_workout_session(label: str) -> bool:
    s = (label or "").lower()
    return any(n in s for n in _WORKOUT_LABEL_NEEDLES)


def _extract_value(f: ExtractedFact) -> tuple[float | None, str | None]:
    """Pull a numeric value (and optional unit) out of a wearable fact.

    Three sources, in order:
      1. coded_concepts.value / .unit
      2. leading number in description
      3. label tail after a colon ("...HeartRateVariabilitySDNN: 28.48 ms")
    """
    value: float | None = None
    unit: str | None = None
    if isinstance(f.coded_concepts, dict):
        v = f.coded_concepts.get("value")
        if isinstance(v, (int, float)):
            value = float(v)
        u = f.coded_concepts.get("unit")
        if isinstance(u, str):
            unit = u
    if value is None and f.description:
        m = re.search(r"-?\d+(?:\.\d+)?", f.description or "")
        if m:
            try:
                value = float(m.group(0))
            except ValueError:
                pass
    if value is None and f.label:
        m = re.search(r":\s*(-?\d+(?:\.\d+)?)", f.label)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                pass
    if unit is None and f.label:
        um = re.search(
            r":\s*-?\d+(?:\.\d+)?\s+([A-Za-z/%][A-Za-z/%0-9]*)",
            f.label,
        )
        if um:
            unit = um.group(1)
    return value, unit


def _aggregate_metrics(facts: list[ExtractedFact]) -> dict[str, Any]:
    """Aggregate wearable facts across a recovery window.

    Output shape:
      {
        # physiological — averaged across samples
        "rhr": {n, mean, min, max, unit},
        "hrv": {...},
        "sleep_duration": {...},
        "vo2max": {...},
        "spo2": {...},

        # endurance — summed across the window AND rolled per day
        "active_energy_kcal": {total, daily_mean, daily_max, active_days, days, unit},
        "exercise_minutes":   {total, daily_mean, daily_max, active_days, days, unit},
        "walking_running_mi": {total, daily_mean, daily_max, active_days, days, unit},
        "steps_total":        {total, daily_mean, daily_max, active_days, days, unit},

        # training context
        "workout_count": {n, durations_sec, days_with_workout},
        "training_gap_days": int,  # longest consecutive zero-activity stretch
      }
    Caught 2026-05-13 PM: previous version reported per-sample MEAN
    for every metric, which made "active energy averaged 0.82 kcal/sample"
    sound like a real fitness signal. Sums and per-day rollups for
    endurance metrics; means stay for HR/HRV/sleep/VO2max/SpO2.
    """
    mean_buckets: dict[str, list[float]] = {}
    mean_units: dict[str, str] = {}
    # day_buckets[metric][date_iso] = sum-of-values-on-that-day
    day_buckets: dict[str, dict[str, float]] = {}
    cum_units: dict[str, str] = {}
    all_active_days: set[str] = set()  # any cumulative metric > 0
    workout_durations: list[float] = []
    workout_days: set[str] = set()

    for f in facts:
        value, unit = _extract_value(f)
        day = f.date_start.date().isoformat() if f.date_start else None

        # Workout sessions
        if _is_workout_session(f.label):
            if value is not None:
                workout_durations.append(value)
            if day:
                workout_days.add(day)
            continue

        # Mean-aggregated physiological metrics
        metric = _classify_wearable(f.label)
        if metric is not None and value is not None:
            mean_buckets.setdefault(metric, []).append(value)
            if unit and metric not in mean_units:
                mean_units[metric] = unit
            continue

        # Cumulative endurance metrics — sum per day
        cum = _classify_cumulative(f.label)
        if cum is not None and value is not None and day is not None:
            day_buckets.setdefault(cum, {}).setdefault(day, 0.0)
            day_buckets[cum][day] += value
            if unit and cum not in cum_units:
                cum_units[cum] = unit
            if value > 0:
                all_active_days.add(day)

    out: dict[str, Any] = {}

    for metric, values in mean_buckets.items():
        out[metric] = {
            "n": len(values),
            "mean": round(sum(values) / len(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "unit": mean_units.get(metric),
        }

    for metric, daily in day_buckets.items():
        days_with_data = len(daily)
        totals = list(daily.values())
        if not totals:
            continue
        active_count = sum(1 for v in totals if v > 0)
        out[metric] = {
            "total": round(sum(totals), 2),
            "daily_mean": round(sum(totals) / days_with_data, 2) if days_with_data else 0,
            "daily_max": round(max(totals), 2),
            "active_days": active_count,
            "days": days_with_data,
            "unit": cum_units.get(metric),
        }

    if workout_durations or workout_days:
        out["workout_count"] = {
            "n": len(workout_durations),
            "durations_sec": [round(d, 1) for d in workout_durations],
            "days_with_workout": sorted(workout_days),
        }

    # Training gap: longest consecutive-day run with no active-energy
    # day in the all_active_days set. Requires the window to know
    # which days are in scope — caller passes by computing the day
    # set from day_buckets. Simpler version: count zero-active days
    # among the days the window actually has data for.
    all_days_seen: set[str] = set()
    for daily in day_buckets.values():
        all_days_seen.update(daily.keys())
    if all_days_seen:
        sorted_days = sorted(all_days_seen)
        max_gap = 0
        cur_gap = 0
        for d in sorted_days:
            if d in all_active_days:
                cur_gap = 0
            else:
                cur_gap += 1
                if cur_gap > max_gap:
                    max_gap = cur_gap
        out["training_gap_days"] = max_gap

    return out


async def _resolve_sources(
    db: AsyncSession,
    facts: list[ExtractedFact],
) -> dict[uuid.UUID, SourceDocument]:
    if not facts:
        return {}
    first_anchor_ids: list[uuid.UUID] = []
    for f in facts:
        if f.evidence_anchor_ids:
            first_anchor_ids.append(f.evidence_anchor_ids[0])
    if not first_anchor_ids:
        return {}
    anc_rows = (await db.execute(
        select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
        .where(EvidenceAnchor.id.in_(first_anchor_ids))
    )).all()
    anchor_to_source = {aid: sid for (aid, sid) in anc_rows if sid is not None}
    sid_list = list(set(anchor_to_source.values()))
    src_by_id: dict[uuid.UUID, SourceDocument] = {}
    if sid_list:
        s_rows = (await db.execute(
            select(SourceDocument).where(SourceDocument.id.in_(sid_list))
        )).scalars().all()
        src_by_id = {s.id: s for s in s_rows}
    out: dict[uuid.UUID, SourceDocument] = {}
    for f in facts:
        first = (f.evidence_anchor_ids or [None])[0]
        if first is None:
            continue
        sid = anchor_to_source.get(first)
        if sid is None:
            continue
        s = src_by_id.get(sid)
        if s is not None:
            out[f.id] = s
    return out


def _fact_dict(f: ExtractedFact, source: SourceDocument | None) -> dict[str, Any]:
    return {
        "fact_id": str(f.id),
        "fact_type": f.fact_type,
        "label": f.label,
        "display_label": f.display_label,
        "date_start": f.date_start.isoformat() if f.date_start else None,
        "extraction_method": f.extraction_method,
        "significance": f.significance,
        "source_id": str(source.id) if source else None,
        "source_name": (
            (source.source_label or source.original_filename) if source else None
        ),
    }


# ---------------------------------------------------------------------------
# Main entry point


async def plan_episode_intelligence(
    db: AsyncSession,
    *,
    fact_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
    natural_language: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the structured payload for one episode (or None if no
    anchor could be resolved). Pure read; no writes."""
    now = now or datetime.now(timezone.utc)
    anchor = await resolve_anchor(
        db, fact_id=fact_id, episode_id=episode_id,
        natural_language=natural_language, now=now,
    )
    if anchor is None:
        return None

    anchor_date = datetime.fromisoformat(anchor["date_start"]) if anchor.get("date_start") else now

    # --- Same-day clinical scope (the surgery + its components) ----
    day_start = anchor_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = anchor_date.replace(hour=23, minute=59, second=59, microsecond=999999)
    day_facts = await _facts_in_window(
        db, day_start, day_end, clinical_only=True, limit=200,
    )
    day_sources = await _resolve_sources(db, day_facts)

    procedure_facts = [f for f in day_facts if f.fact_type == "procedure"]
    condition_facts = [f for f in day_facts if f.fact_type == "condition"]
    encounter_facts = [f for f in day_facts if f.fact_type == "encounter"]

    # A "named" medication has a label other than a bare FHIR reference
    # ("MedicationRequest e0nrm..." with an opaque ID). Those rows are
    # MedicationRequest resources whose medicationReference wasn't
    # resolved during ingest — the Medication.code.text or display name
    # never made it into the fact. Treat unresolved ones as a separate
    # bucket so the LLM can honestly say "N MedicationRequest entries
    # exist but I can't name them" instead of either claiming 24
    # anesthesia agents or pretending the records don't exist.
    def _is_named_med(f: ExtractedFact) -> bool:
        label = (f.label or "").strip()
        return not (
            label.startswith("MedicationRequest ")
            or label.startswith("MedicationStatement ")
            or label.startswith("Medication ") and len(label.split()) <= 2
        )

    all_meds = [f for f in day_facts if f.fact_type == "medication"]
    unresolved_med_refs = [f for f in all_meds if not _is_named_med(f)]
    named_meds = [f for f in all_meds if _is_named_med(f)]
    anesthesia_facts = [f for f in named_meds if _is_anesthesia(f)]
    perioperative_support_facts = [
        f for f in named_meds
        if not _is_anesthesia(f) and _is_perioperative_support(f)
    ]
    # "Other meds same day" excludes anesthesia AND perioperative
    # support AND unresolved references to avoid double-listing —
    # the narrative cites each bucket separately.
    other_meds = [
        f for f in named_meds
        if not _is_anesthesia(f) and not _is_perioperative_support(f)
    ]

    # Episode kind hint — used to pick the recovery profile.
    # V1: when the anchor is a procedure, assume surgery; otherwise
    # fall through to default. Promotion to a canonical Episode
    # captures the explicit kind on Episode.kind; the planner reads
    # that path in plan_for_episode().
    anchor_significance = anchor.get("significance") or ""
    if "procedure" in anchor_significance:
        episode_kind_hint = "surgery"
    else:
        episode_kind_hint = "default"

    # --- ±21 day surrounding window: travel, life context, calendar
    # Exclude wearable observations for the same reason as the
    # same-day query — six weeks of HK data easily blows past any
    # limit and starves out the travel / life-event facts.
    surround_start = anchor_date - timedelta(days=21)
    surround_end = anchor_date + timedelta(days=21)
    surround_facts = await _facts_in_window(
        db, surround_start, surround_end, clinical_only=True, limit=500,
    )
    travel_life_facts = [f for f in surround_facts if _is_travel_or_life(f)]
    travel_sources = await _resolve_sources(db, travel_life_facts)

    # --- Recovery windows from the episode-kind profile ---------------
    window_data: list[dict[str, Any]] = []
    for name, days_a, days_b in _profile_for_kind(episode_kind_hint):
        start = anchor_date + timedelta(days=days_a)
        end = anchor_date + timedelta(days=days_b, hours=23, minutes=59, seconds=59)
        wearable_facts = await _facts_in_window(
            db, start, end, wearable_only=True, limit=20000,
        )
        metrics = _aggregate_metrics(wearable_facts)
        window_data.append({
            "name": name,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "wearable_fact_count": len(wearable_facts),
            "metrics": metrics,
        })

    # --- Sources contributing to this episode --------------------------
    all_sources: dict[uuid.UUID, SourceDocument] = {**day_sources, **travel_sources}
    sources_out = [
        {
            "source_id": str(s.id),
            "source_name": s.source_label or s.original_filename,
            "source_type": s.source_type,
        }
        for s in all_sources.values()
    ]

    return {
        "anchor": anchor,
        "what_happened": {
            "procedures": [_fact_dict(f, day_sources.get(f.id)) for f in procedure_facts],
            "conditions": [_fact_dict(f, day_sources.get(f.id)) for f in condition_facts],
            "encounters": [_fact_dict(f, day_sources.get(f.id)) for f in encounter_facts],
            "sources": sources_out,
        },
        "anesthesia_meds": {
            "facts": [_fact_dict(f, day_sources.get(f.id)) for f in anesthesia_facts],
            "missing_anesthesia_record": len(anesthesia_facts) == 0,
            "other_meds_same_day": [
                _fact_dict(f, day_sources.get(f.id)) for f in other_meds[:25]
            ],
            # Same-day MedicationRequest facts that reference a
            # Medication resource we never resolved during ingest.
            # They EXIST but we can't name them. The LLM should
            # state this fact, not claim the meds aren't recorded
            # at all and not invent agent names.
            "unresolved_medication_references": {
                "count": len(unresolved_med_refs),
                "samples": [
                    _fact_dict(f, day_sources.get(f.id))
                    for f in unresolved_med_refs[:5]
                ],
            },
        },
        "perioperative_support_meds": {
            "facts": [
                _fact_dict(f, day_sources.get(f.id))
                for f in perioperative_support_facts
            ],
            "category_explanation": (
                "Anti-emetics, multimodal analgesia, corticosteroids, and "
                "induction antibiotics. Distinct from the anesthetic agents."
            ),
        },
        "episode_kind_hint": episode_kind_hint,
        "travel_and_life": {
            "events": [
                _fact_dict(f, travel_sources.get(f.id)) for f in travel_life_facts
            ],
            "window_days": 21,
        },
        "body_response": {
            "windows": window_data,
        },
        "follow_up_questions": _seed_follow_ups(anchor, anesthesia_facts),
    }


def _seed_follow_ups(anchor: dict[str, Any], anesthesia_facts: list[ExtractedFact]) -> list[str]:
    qs: list[str] = []
    label = anchor.get("label") or "this event"
    qs.append(f"Show me the source records that document {label}.")
    if anesthesia_facts:
        qs.append("Tell me more about the intraoperative anesthesia meds.")
    else:
        qs.append("Where would the anesthesia record live and can we ingest it?")
    qs.append("Compare this recovery to a previous similar event.")
    qs.append("Create a recovery episode for this so I can revisit it.")
    return qs
