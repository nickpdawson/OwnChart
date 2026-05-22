"""Wearable summary retrieval (Beta 1, FU-ASK-RECENT-WEARABLE
follow-on, 2026-05-22).

For health-pattern questions ("compare my sleep, HRV, resting HR,
training last week"), the substring + category passes in
``search_facts`` return 40 raw per-sample rows ordered by trigram
similarity — useless context for the LLM to summarize a week.

This module:

  1. Detects whether the question is a wearable-pattern query
     via the existing observation synonym set
     (``_FACT_TYPE_SYNONYMS["observation"]``).

  2. Pulls native_healthkit + health_auto_export rows for the
     requested time window (default trailing 7 days; caller may
     pass a TemporalWindow from ``retrieval/temporal.py``).

  3. Classifies each row's metric_type from the label prefix
     (``hkquantitytypeidentifierrestingheartrate: 61.00 count/min``
     → ``resting_heart_rate``). Heuristic-only — no ML, no LLM.

  4. Parses the per-row scalar value from the label suffix when
     present and aggregates per (day, metric):
       - average / min / max for instantaneous metrics (HR, HRV)
       - sum for cumulative metrics (steps, active energy)
       - count for category metrics (sleep, workouts)

  5. Renders a compact ``## Wearable summary`` markdown block
     with one row per (day, metric) — typically 30-50 lines for
     a 7-day window, instead of 600+ raw rows.

  6. Emits count-only telemetry: rows fetched per metric, summary
     rows emitted, span days. No values, no PHI.

Pure-ish: takes an AsyncSession; returns the summary dict and a
formatted block. The detector and parser are pure functions and
trivially testable.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.extracted_fact import ExtractedFact

log = get_logger("ownchart.retrieval.wearable_summary")


# ---------------------------------------------------------------------------
# Metric classifier
#
# Labels from native_healthkit follow the pattern
#   ``hk<type>identifier<metric>[: <value> <unit>]``
# e.g.
#   ``hkquantitytypeidentifierrestingheartrate: 61.00 count/min``
#   ``hkcategorytypeidentifiersleepanalysis``
#   ``hkquantitytypeidentifierheartratevariabilitysdnn: 42.5 ms``
#
# health_auto_export uses human-readable labels:
#   ``heart rate: avg 63, min 63, max 63 count/min``
#   ``resting energy: 1 kcal``
#   ``active energy: 0 kcal``
#   ``walking + running: 0.00 mi``
#   ``flights climbed: 0``
#   ``stand time: 0 min``
#   ``exercise time: 1 min``
#
# Workouts (native_healthkit): ``running — 51 min, 7.6 km, 546 kcal``
# and similar; surfaced as their own metric_type ``workout``.


# (metric_type, label_substring_to_match). First match wins.
_METRIC_PATTERNS: tuple[tuple[str, str], ...] = (
    # Native HK (most specific first)
    ("sleep", "hkcategorytypeidentifiersleepanalysis"),
    ("heart_rate_variability", "hkquantitytypeidentifierheartratevariability"),
    ("resting_heart_rate", "hkquantitytypeidentifierrestingheartrate"),
    ("walking_heart_rate", "hkquantitytypeidentifierwalkingheartrateaverage"),
    ("vo2_max", "hkquantitytypeidentifiervo2max"),
    ("oxygen_saturation", "hkquantitytypeidentifieroxygensaturation"),
    ("heart_rate", "hkquantitytypeidentifierheartrate"),
    ("steps", "hkquantitytypeidentifierstepcount"),
    ("active_energy", "hkquantitytypeidentifieractiveenergyburned"),
    ("basal_energy", "hkquantitytypeidentifierbasalenergyburned"),
    ("distance", "hkquantitytypeidentifierdistancewalkingrunning"),
    ("flights", "hkquantitytypeidentifierflightsclimbed"),
    # Workout shapes
    ("workout", "hkworkouttype"),
    ("workout", "hkworkoutroutetype"),
    # Auto Export human-readable
    ("heart_rate", "heart rate"),
    ("resting_energy", "resting energy"),
    ("active_energy", "active energy"),
    ("distance", "walking + running"),
    ("flights", "flights climbed"),
    ("stand_time", "stand time"),
    ("exercise_time", "exercise time"),
    ("steps", "step"),  # "step count", "daily steps"
    # Workout labels from health_auto_export look like
    # "running — 51 min, 7.6 km, 546 kcal".
    ("workout", "running —"),
    ("workout", "cycling —"),
    ("workout", "walking —"),
    ("workout", "swimming —"),
    ("workout", "hiking —"),
)


# How each metric_type aggregates per day.
#   ``avg``                       — mean of the parsed scalar values
#   ``sum``                       — sum of values (cumulative)
#   ``count``                     — count of rows (no value parse)
#   ``count_with_duration``       — count of rows + sum of "X min"
#   ``duration_from_timestamps``  — sum of (date_end - date_start) per
#                                   segment in minutes. Apple HK
#                                   sleep rows carry no value/unit in
#                                   the label; the segment span is
#                                   the only reliable duration source.
_METRIC_AGGREGATION: dict[str, str] = {
    "sleep": "duration_from_timestamps",
    "workout": "count_with_duration",
    "heart_rate_variability": "avg",
    "resting_heart_rate": "avg",
    "walking_heart_rate": "avg",
    "heart_rate": "avg",
    "vo2_max": "avg",
    "oxygen_saturation": "avg",
    "steps": "sum",
    "active_energy": "sum",
    "basal_energy": "sum",
    "resting_energy": "sum",
    "distance": "sum",
    "flights": "sum",
    "stand_time": "sum",
    "exercise_time": "sum",
}


# Display scaling — for metrics where Apple HK stores a fraction
# (0.0–1.0) but the human-readable form is a percent. The summary
# formatter multiplies the aggregate by the factor and overrides
# the unit display. Add metrics here, NOT in the aggregation step —
# the underlying min/max/avg arithmetic must stay in source units
# so downstream consumers can re-derive.
_DISPLAY_SCALE: dict[str, tuple[float, str]] = {
    # Apple HK SpO2 is stored as a 0.0–1.0 fraction; users read it
    # as a percent. avg=0.972 → avg=97.2%. Caught 2026-05-22 from
    # the model's own "0.9% is a unit-display quirk" callout.
    "oxygen_saturation": (100.0, "%"),
}


# Vocabulary that should trigger the wearable summary pass.
#
# PM doctrine (2026-05-22 evening, round-3): the trigger should be
# metric / source / intent based, not dependent on a PM taxonomy
# phrase. A user is more likely to say "my numbers" or "Apple Health"
# than "wearable data". Three buckets below — generic intent,
# source/device names, specific metrics. Adding a new entry to any
# bucket should not require touching the detector or formatter.
#
# Care: vocabulary words that ALSO appear in the EHR clinical
# observation bucket (e.g. "vitals") are intentionally listed here
# too. The wearable summary pass JOINs on extraction_method IN
# ('native_healthkit', 'health_auto_export'), so even when the
# trigger fires, only wearable rows enter the summary block;
# clinical observation rows still flow through search_facts to the
# fact_block. Both can appear in the prompt and that's fine.
_WEARABLE_TRIGGER_TOKENS: frozenset[str] = frozenset({
    # ----- Generic intent / category nouns
    "wearable", "wearables", "wearable data",
    "body data", "body signal", "body signals", "body signal data",
    "health data", "health metrics", "health signals",
    "physiologic signals", "physiological signals",
    "fitness", "fitness data", "fitness tracker",
    "activity data", "recovery data",
    "device data", "watch data",
    "my numbers", "my stats", "my metrics",
    "vitals",  # also lives in observation synonyms — see note above
    # ----- Sources / devices
    "healthkit", "health kit", "apple health", "apple watch",
    "whoop", "garmin", "fitbit", "oura", "withings", "omron",
    "dexcom", "libre", "freestyle libre",
    "cgm", "continuous glucose monitor",
    "bp cuff", "blood pressure cuff", "smart scale",
    "sleep tracker", "ring",
    # ----- Sleep
    "sleep", "sleeping", "slept", "sleep data", "sleep stages",
    # ----- Cardiac / cardiovascular
    "hrv", "heart rate variability",
    "heart rate", "resting heart rate", "resting hr", "rhr",
    "walking heart rate", "pulse",
    "blood pressure", "bp",
    # ----- Respiratory / oxygen
    "spo2", "oxygen", "oxygen saturation",
    "vo2 max", "vo2max",
    # ----- Activity / training
    "workout", "workouts", "training", "trained",
    "exercise", "exercises", "exercising",
    "steps", "step count", "stand", "stand time",
    "activity", "activities",
    "distance", "walking + running",
    "calories", "energy", "active energy", "basal energy",
    "exercise time",
    "recovery", "readiness", "strain",
    # ----- Body composition / metabolic
    "weight", "body weight",
    "glucose", "blood sugar", "blood glucose",
})


def question_is_wearable_pattern(question: str) -> bool:
    """True iff the question contains any wearable-trigger vocabulary.

    Used by the chat path to decide whether to spend the wearable
    summary fetch. False on pure clinical questions ("what surgeries
    have I had") even though "vital" is in the broader observation
    synonym set.
    """
    if not question or not isinstance(question, str):
        return False
    q = question.lower()
    # Multi-word phrase check.
    for phrase in _WEARABLE_TRIGGER_TOKENS:
        if " " in phrase and phrase in q:
            return True
    # Token-level check.
    tokens = re.split(r"[^\w]+", q)
    for tok in tokens:
        if tok in _WEARABLE_TRIGGER_TOKENS:
            return True
    return False


def classify_metric(label: str) -> str | None:
    """Return the metric_type for a wearable fact label, or None
    when the label doesn't match a known pattern."""
    if not label:
        return None
    lab = label.lower()
    for metric, marker in _METRIC_PATTERNS:
        if marker in lab:
            return metric
    return None


# Match "<prefix>: <number> <unit>" where number can be int / float,
# optionally negative, with optional decimals.
_LABEL_VALUE_RE = re.compile(
    r":\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%+\-]+)?$"
)
# Match "X min" inside a workout summary like "running — 51 min, 7.6 km, ...".
_DURATION_MIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*min\b")


def parse_scalar_value(label: str) -> tuple[float, str | None] | None:
    """Parse the trailing ``: <number> <unit>`` from a HK / Auto
    Export label. Returns (value, unit) or None when the label
    has no scalar suffix (e.g. sleep category rows)."""
    if not label:
        return None
    m = _LABEL_VALUE_RE.search(label)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except (ValueError, TypeError):
        return None
    unit = m.group(2)
    return v, unit


def parse_duration_minutes(label: str) -> float | None:
    """Parse the first ``<n> min`` in a label. Used for workouts
    where the label is the per-workout summary (e.g.
    ``running — 51 min, 7.6 km, 546 kcal``)."""
    if not label:
        return None
    m = _DURATION_MIN_RE.search(label.lower())
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Aggregation


@dataclass
class _PerMetricBucket:
    values: list[float] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    durations_min: list[float] = field(default_factory=list)
    # (start, end) intervals for sleep + workout duration dedupe
    # (FU-SLEEP-SEGMENT-DEDUP 2026-05-22). Apple HK writes the same
    # sleep night from multiple sources (Apple Watch + iPhone +
    # third-party apps) AND splits each night into InBed vs
    # AsleepCore/REM/Deep stage rows that all overlap. Summing
    # raw durations gave 220h "sleep" per day. Intervals are
    # union-merged before summing so the same wall-clock window
    # only counts once.
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    row_count: int = 0


def _merge_intervals_minutes(
    intervals: list[tuple[datetime, datetime]],
) -> float:
    """Union-merge overlapping or touching intervals; return total
    minutes of the merged set.

    Sort by start time, sweep forward, extending the current span
    whenever the next interval starts at-or-before the current
    end. Identical intervals collapse to one. Touching intervals
    (``cur_end == next_start``) merge — Apple HK occasionally
    splits a sleep window at the second boundary.
    """
    if not intervals:
        return 0.0
    sorted_iv = sorted(intervals, key=lambda x: x[0])
    cur_start, cur_end = sorted_iv[0]
    merged_seconds = 0.0
    for start, end in sorted_iv[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
        else:
            merged_seconds += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
    merged_seconds += (cur_end - cur_start).total_seconds()
    return merged_seconds / 60.0


@dataclass
class WearableDaySummary:
    day: date
    metric: str
    aggregation: str  # mirror of _METRIC_AGGREGATION
    row_count: int
    avg: float | None = None
    min: float | None = None
    max: float | None = None
    sum: float | None = None
    duration_min_sum: float | None = None
    unit: str | None = None
    # Data-quality flag (FU-SLEEP-PLAUSIBILITY 2026-05-22).
    # When set, the summary surfaces the day's data as
    # "implausible_sleep_duration" rather than passing it through
    # as a normal sleep figure. Examples: a sleep night whose
    # merged duration still exceeds the physiologic ceiling
    # (~18h) after interval-merge — typically caused by sources
    # whose interval boundaries don't actually overlap (e.g. one
    # source records UTC-naive while another records a
    # different-tz wall-clock window).
    quality_flag: str | None = None


# Sleep plausibility ceiling. Even with interval dedupe, anything
# over this is a data-quality condition, not a real night. Surface
# it instead of silently capping (PM-required, 2026-05-22).
_IMPLAUSIBLE_SLEEP_MINUTES: float = 18 * 60  # 1080 minutes / 18h


def _summarize_bucket(
    day: date, metric: str, bucket: _PerMetricBucket,
) -> WearableDaySummary:
    agg = _METRIC_AGGREGATION.get(metric, "count")
    unit = bucket.units[0] if bucket.units else None
    s = WearableDaySummary(
        day=day,
        metric=metric,
        aggregation=agg,
        row_count=bucket.row_count,
        unit=unit,
    )
    if bucket.values:
        s.min = min(bucket.values)
        s.max = max(bucket.values)
        s.avg = sum(bucket.values) / len(bucket.values)
        s.sum = sum(bucket.values)
    # Duration: union-merge timestamp intervals (sleep + workout),
    # then add any label-only durations (Auto Export workout
    # summary lines). When neither source is populated,
    # duration_min_sum stays None and the renderer falls back to
    # row count.
    if bucket.intervals or bucket.durations_min:
        merged = _merge_intervals_minutes(bucket.intervals)
        labeled = sum(bucket.durations_min)
        s.duration_min_sum = merged + labeled

    # Sleep plausibility guardrail (FU-SLEEP-PLAUSIBILITY 2026-05-22).
    # Even after interval-merge, a value over ~18h/night is a
    # data-quality condition, not a real night. Surface as a
    # quality flag rather than silently capping — the raw merged
    # minutes stay visible in s.duration_min_sum so an operator
    # can debug without re-running the query.
    if (
        metric == "sleep"
        and s.duration_min_sum is not None
        and s.duration_min_sum > _IMPLAUSIBLE_SLEEP_MINUTES
    ):
        s.quality_flag = "implausible_sleep_duration"
    return s


async def summarize_wearable_window(
    db: AsyncSession,
    *,
    person_record_id: uuid.UUID,
    time_min: datetime,
    time_max: datetime,
) -> tuple[list[WearableDaySummary], dict[str, int]]:
    """Aggregate native_healthkit + health_auto_export rows in
    ``(time_min, time_max)`` per (day, metric).

    Returns (summaries, telemetry_counts) where telemetry_counts
    is a count-only dict suitable for log emission (no PHI).
    Summaries are ordered (day desc, metric asc) so the most
    recent day comes first in the prompt block.
    """
    # Pull all wearable rows in window. Cap is generous because we
    # collapse to one row per (day, metric); the raw count can be
    # high without exploding the prompt.
    #
    # date_end is included so duration-from-timestamps works for
    # sleep + workout segments — Apple HK sleep rows have no
    # duration string in the label, so (date_end - date_start) is
    # the only reliable source. Caught 2026-05-22 PM after the
    # model surfaced "sleep: count=N rows are sleep-sample counts,
    # not hours".
    stmt = (
        select(
            ExtractedFact.label,
            ExtractedFact.date_start,
            ExtractedFact.date_end,
            ExtractedFact.extraction_method,
        )
        .where(ExtractedFact.person_record_id == person_record_id)
        .where(ExtractedFact.extraction_method.in_(
            ("native_healthkit", "health_auto_export"),
        ))
        .where(ExtractedFact.date_start >= time_min)
        .where(ExtractedFact.date_start <= time_max)
        .order_by(ExtractedFact.date_start.desc())
        .limit(10_000)
    )
    rows = (await db.execute(stmt)).all()

    # Aggregate.
    buckets: dict[tuple[date, str], _PerMetricBucket] = defaultdict(_PerMetricBucket)
    rows_per_metric_in: dict[str, int] = defaultdict(int)
    rows_classified_total = 0
    rows_unclassified = 0
    for label, dt, dt_end, _em in rows:
        if dt is None:
            continue
        metric = classify_metric(label or "")
        if metric is None:
            rows_unclassified += 1
            continue
        rows_classified_total += 1
        rows_per_metric_in[metric] += 1
        day = dt.date()
        key = (day, metric)
        b = buckets[key]
        b.row_count += 1
        scalar = parse_scalar_value(label or "")
        if scalar is not None:
            v, unit = scalar
            b.values.append(v)
            if unit:
                b.units.append(unit)
        # Duration: prefer (date_end - date_start) — works for
        # Apple HK sleep + workout segments where the label has
        # no duration. Collect as (start, end) intervals so the
        # summary step can union-merge them (FU-SLEEP-SEGMENT-DEDUP):
        # the same sleep night can be written by 4+ sources × 4
        # sleep stages and summing raw durations multiplies the
        # real span. Fall back to label parsing ("X min") for
        # Auto Export workout summary lines that don't have a
        # date_end — those are already non-overlapping per-session
        # summaries and are added as-is.
        if metric in ("sleep", "workout"):
            if dt_end is not None and dt_end > dt:
                b.intervals.append((dt, dt_end))
            else:
                ts_minutes = parse_duration_minutes(label or "")
                if ts_minutes is not None:
                    b.durations_min.append(ts_minutes)

    summaries = [
        _summarize_bucket(day, metric, b)
        for (day, metric), b in buckets.items()
    ]
    # Most recent day first; within a day, alphabetical metric for
    # stable ordering.
    summaries.sort(key=lambda s: (-s.day.toordinal(), s.metric))

    telemetry = {
        "rows_total": len(rows),
        "rows_classified": rows_classified_total,
        "rows_unclassified": rows_unclassified,
        "summary_rows_emitted": len(summaries),
        "span_days": _span_days(time_min, time_max),
    }
    # Per-metric raw counts (no values).
    telemetry.update({
        f"raw_rows_{k}": v for k, v in rows_per_metric_in.items()
    })
    return summaries, telemetry


def _span_days(time_min: datetime, time_max: datetime) -> int:
    delta = time_max - time_min
    return max(0, int(delta.total_seconds() // 86400) + 1)


# ---------------------------------------------------------------------------
# Render


def format_wearable_summary_block(
    summaries: list[WearableDaySummary],
    *,
    phrase: str | None = None,
) -> str:
    """Render the summary list as a markdown block the LLM can
    consume. Empty input → empty string for splice-friendly concat.

    ``phrase`` is the matched temporal-window phrase (e.g.
    "last week") — surfaced in the header so the LLM grounds its
    answer in the exact window the user asked about.
    """
    if not summaries:
        return ""
    header = "## Wearable summary"
    if phrase:
        header += f" (window: {phrase})"
    lines: list[str] = ["", header]
    current_day: date | None = None
    for s in summaries:
        if s.day != current_day:
            lines.append(f"- {s.day.isoformat()}:")
            current_day = s.day
        lines.append(f"  - {s.metric}: {_summary_value_text(s)}")
    return "\n".join(lines)


def _format_hours_minutes(total_minutes: float) -> str:
    """Render a minute count as "Xh Ym" — friendlier than "432 min"
    for sleep totals. Rounds to nearest minute."""
    total = max(0, int(round(total_minutes)))
    h, m = divmod(total, 60)
    if h == 0:
        return f"{m}m"
    return f"{h}h {m}m"


def _scaled_value_and_unit(
    metric: str, value: float, unit: str | None,
) -> tuple[float, str]:
    """Apply ``_DISPLAY_SCALE`` for metrics whose storage form is
    not the human-readable form (Apple HK SpO2: 0.0–1.0 → 0–100%).
    Returns the scaled value + display unit (which may override
    the stored unit string)."""
    scale = _DISPLAY_SCALE.get(metric)
    if scale is None:
        return value, (unit or "")
    factor, display_unit = scale
    return value * factor, display_unit


def _summary_value_text(s: WearableDaySummary) -> str:
    """One-line text for a (day, metric) summary. Picks the
    aggregation that matches the metric's semantics."""
    parts: list[str] = []
    if s.aggregation == "avg" and s.avg is not None:
        avg_v, unit = _scaled_value_and_unit(s.metric, s.avg, s.unit)
        unit_s = f" {unit}" if unit else ""
        parts.append(f"avg={avg_v:.1f}{unit_s}")
        if s.min is not None and s.max is not None and s.min != s.max:
            min_v, _ = _scaled_value_and_unit(s.metric, s.min, s.unit)
            max_v, _ = _scaled_value_and_unit(s.metric, s.max, s.unit)
            parts.append(f"min={min_v:.1f}, max={max_v:.1f}")
    elif s.aggregation == "sum" and s.sum is not None:
        sum_v, unit = _scaled_value_and_unit(s.metric, s.sum, s.unit)
        unit_s = f" {unit}" if unit else ""
        parts.append(f"total={sum_v:.1f}{unit_s}")
    elif s.aggregation == "duration_from_timestamps":
        # Sleep — render as "duration=7h 12m" from summed segment
        # spans. Skip the sample count; it isn't user-meaningful
        # for sleep (Apple HK emits many short stage rows per night).
        #
        # When the day is flagged implausible (merged total >
        # _IMPLAUSIBLE_SLEEP_MINUTES even after dedupe), do NOT
        # present the value as a normal sleep figure. Surface the
        # quality flag + raw merged minutes for debugging, and a
        # short explanation. The PM directive (2026-05-22) — do not
        # silently cap; the merged total stays visible so an
        # operator can trace the upstream ingestion gap.
        if s.quality_flag == "implausible_sleep_duration":
            assert s.duration_min_sum is not None  # set together
            parts.append(
                f"quality=implausible_sleep_duration "
                f"(merged={_format_hours_minutes(s.duration_min_sum)} "
                f"from {s.row_count} segments; "
                f"overlapping sleep sources or duplicate segments likely)"
            )
        elif s.duration_min_sum is not None and s.duration_min_sum > 0:
            parts.append(f"duration={_format_hours_minutes(s.duration_min_sum)}")
        else:
            # No usable timestamps — surface the gap honestly.
            parts.append(f"segments={s.row_count} (no durations)")
    elif s.aggregation == "count_with_duration":
        if s.duration_min_sum is not None and s.duration_min_sum > 0:
            parts.append(
                f"count={s.row_count}, "
                f"duration={_format_hours_minutes(s.duration_min_sum)}"
            )
        else:
            parts.append(f"count={s.row_count}")
    else:
        parts.append(f"count={s.row_count}")
    return ", ".join(parts)
