"""Wearable summary retrieval tests (FU-ASK-RECENT-WEARABLE
follow-on, 2026-05-22).

Pure-function coverage of the metric classifier, value parser,
aggregation, and prompt-block renderer. The async
``summarize_wearable_window`` integration is exercised via the
conversations-path static-source pin in
``test_conversations_calendar_integration.py``; this file is
DB-free.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ownchart.retrieval.wearable_summary import (
    WearableDaySummary,
    _METRIC_AGGREGATION,
    _merge_intervals_minutes,
    _summarize_bucket,
    _PerMetricBucket,
    classify_metric,
    format_wearable_summary_block,
    parse_duration_minutes,
    parse_scalar_value,
    question_is_wearable_pattern,
)


# ---------------------------------------------------------------------------
# question_is_wearable_pattern — trigger vocabulary


@pytest.mark.parametrize(
    "question",
    [
        "compare my sleep last week",
        "what is my HRV trend",
        "show me HRV and resting heart rate",
        "training load past 7 days",
        "how many workouts did I do",
        "step count yesterday",
        "active energy this week",
        "compare my sleep, HRV, resting HR, and training",
        # PM-caught 2026-05-22 evening — the device-data nouns
        # the user actually says.
        "what was my schedule like last week and did it correlate "
        "to my wearable data?",
        "summarize my wearable data for this week",
        "how do my wearables look",
        "show me my HealthKit data",
        "summarize my apple health data",
        "what's my body data showing",
        "device data for the past 14 days",
        "show me my fitness data",
        "summarize my fitness this week",
        "what does my recovery look like",
        "my readiness scores past week",
        "whoop summary",
        "garmin data",
        "fitbit data",
    ],
)
def test_wearable_pattern_detected(question):
    assert question_is_wearable_pattern(question) is True


def test_wearable_pattern_exact_pm_failing_question():
    """The exact question Nick sent that did NOT trigger under
    the prior trigger set. Pin it explicitly so a future refactor
    can't silently break it again."""
    q = (
        "What was my schedule like last week and did it correlate "
        "to my wearable data?"
    )
    assert question_is_wearable_pattern(q) is True


@pytest.mark.parametrize(
    "question",
    [
        "what medications am I on",
        "tell me about my eye surgery",
        "list my providers",
        "when was my last lab work",
        "what conditions are on my problem list",
    ],
)
def test_non_wearable_questions_not_detected(question):
    assert question_is_wearable_pattern(question) is False


def test_wearable_pattern_handles_none_and_empty():
    assert question_is_wearable_pattern("") is False
    assert question_is_wearable_pattern(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classify_metric — label-prefix routing


@pytest.mark.parametrize(
    "label, expected",
    [
        ("hkcategorytypeidentifiersleepanalysis", "sleep"),
        (
            "hkquantitytypeidentifierheartratevariabilitysdnn: 42.5 ms",
            "heart_rate_variability",
        ),
        (
            "hkquantitytypeidentifierrestingheartrate: 61.00 count/min",
            "resting_heart_rate",
        ),
        (
            "hkquantitytypeidentifierwalkingheartrateaverage: 110.0 count/min",
            "walking_heart_rate",
        ),
        ("hkquantitytypeidentifiervo2max: 42.1 ml/kg/min", "vo2_max"),
        ("hkquantitytypeidentifieroxygensaturation: 0.98", "oxygen_saturation"),
        ("hkquantitytypeidentifierheartrate: 72.0 count/min", "heart_rate"),
        ("hkquantitytypeidentifierstepcount: 8542", "steps"),
        (
            "hkquantitytypeidentifieractiveenergyburned: 540.0 kcal",
            "active_energy",
        ),
        (
            "hkquantitytypeidentifierdistancewalkingrunning: 5.2 km",
            "distance",
        ),
        ("hkquantitytypeidentifierflightsclimbed: 12", "flights"),
        ("hkworkouttype", "workout"),
        ("hkworkoutroutetype", "workout"),
        # Auto Export human-readable
        ("heart rate: avg 63, min 60, max 72 count/min", "heart_rate"),
        ("resting energy: 1 kcal", "resting_energy"),
        ("active energy: 540 kcal", "active_energy"),
        ("walking + running: 5.2 mi", "distance"),
        ("flights climbed: 12", "flights"),
        ("stand time: 60 min", "stand_time"),
        ("exercise time: 30 min", "exercise_time"),
        # Workout summary labels
        ("running — 51 min, 7.6 km, 546 kcal", "workout"),
        ("cycling — 23 min, 7.3 km, 89 kcal", "workout"),
    ],
)
def test_classify_metric(label, expected):
    assert classify_metric(label) == expected


def test_classify_metric_unknown_label_returns_none():
    assert classify_metric("some random clinical fact") is None
    assert classify_metric("") is None
    assert classify_metric(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_scalar_value — extract trailing value + unit


def test_parse_scalar_value_simple_int():
    out = parse_scalar_value(
        "hkquantitytypeidentifierstepcount: 8542 count"
    )
    assert out == (8542.0, "count")


def test_parse_scalar_value_float_with_unit():
    out = parse_scalar_value(
        "hkquantitytypeidentifierrestingheartrate: 61.00 count/min"
    )
    assert out == (61.00, "count/min")


def test_parse_scalar_value_no_unit():
    out = parse_scalar_value("hkquantitytypeidentifierflightsclimbed: 12")
    assert out is not None
    val, unit = out
    assert val == 12.0
    assert unit is None or unit == ""


def test_parse_scalar_value_returns_none_for_label_without_suffix():
    assert parse_scalar_value("hkcategorytypeidentifiersleepanalysis") is None
    assert parse_scalar_value("hkworkouttype") is None


def test_parse_scalar_value_handles_empty_and_none():
    assert parse_scalar_value("") is None
    assert parse_scalar_value(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_duration_minutes — extract "X min" from workout labels


def test_parse_duration_minutes_from_workout_label():
    assert parse_duration_minutes("running — 51 min, 7.6 km, 546 kcal") == 51.0
    assert parse_duration_minutes("cycling — 23 min, 7.3 km") == 23.0


def test_parse_duration_minutes_decimal():
    assert parse_duration_minutes("walking — 45.5 min, 3.2 km") == 45.5


def test_parse_duration_minutes_no_match_returns_none():
    assert parse_duration_minutes("heart rate: avg 63") is None
    assert parse_duration_minutes("") is None


# ---------------------------------------------------------------------------
# Aggregation


def test_aggregation_avg_metric_uses_mean_min_max():
    b = _PerMetricBucket(values=[60.0, 65.0, 70.0], row_count=3)
    s = _summarize_bucket(date(2026, 5, 22), "heart_rate", b)
    assert s.aggregation == "avg"
    assert s.avg == pytest.approx(65.0)
    assert s.min == 60.0
    assert s.max == 70.0


def test_aggregation_sum_metric_uses_sum():
    b = _PerMetricBucket(values=[100.0, 200.0, 300.0], row_count=3)
    s = _summarize_bucket(date(2026, 5, 22), "active_energy", b)
    assert s.aggregation == "sum"
    assert s.sum == 600.0


def test_aggregation_sleep_uses_duration_from_timestamps():
    """PM-corrected 2026-05-22: sleep aggregates via
    duration_from_timestamps (sum of segment spans), NOT
    count_with_duration. Sleep labels carry no duration string
    so the only reliable source is date_end - date_start."""
    b = _PerMetricBucket(
        values=[], durations_min=[420.0], row_count=4,
    )
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    assert s.aggregation == "duration_from_timestamps"
    assert s.row_count == 4
    assert s.duration_min_sum == 420.0


def test_aggregation_workout_uses_count_with_duration():
    b = _PerMetricBucket(
        values=[],
        durations_min=[51.0, 30.0],
        row_count=2,
    )
    s = _summarize_bucket(date(2026, 5, 22), "workout", b)
    assert s.aggregation == "count_with_duration"
    assert s.row_count == 2
    assert s.duration_min_sum == 81.0


def test_aggregation_uses_first_unit_when_present():
    b = _PerMetricBucket(
        values=[60.0, 65.0],
        units=["count/min", "count/min"],
        row_count=2,
    )
    s = _summarize_bucket(date(2026, 5, 22), "heart_rate", b)
    assert s.unit == "count/min"


def test_aggregation_handles_zero_rows():
    b = _PerMetricBucket()
    s = _summarize_bucket(date(2026, 5, 22), "heart_rate", b)
    assert s.row_count == 0
    assert s.avg is None and s.sum is None


# ---------------------------------------------------------------------------
# Aggregation registry pin — required metrics PM listed
# (sleep, HRV, RHR, workouts/training, active energy, steps)


@pytest.mark.parametrize(
    "metric, expected_agg",
    [
        # PM-corrected 2026-05-22: sleep now uses
        # duration_from_timestamps (sum of segment spans), NOT
        # count_with_duration. HK sleep labels carry no duration.
        ("sleep", "duration_from_timestamps"),
        ("workout", "count_with_duration"),
        ("heart_rate_variability", "avg"),
        ("resting_heart_rate", "avg"),
        ("heart_rate", "avg"),
        ("steps", "sum"),
        ("active_energy", "sum"),
        ("exercise_time", "sum"),
    ],
)
def test_required_metrics_have_aggregation(metric, expected_agg):
    """PM directive enumerates these metrics for "compare last week"
    questions. Each must be in the aggregation registry."""
    assert _METRIC_AGGREGATION.get(metric) == expected_agg


# ---------------------------------------------------------------------------
# Render


def test_format_wearable_summary_block_groups_by_day():
    """Block groups summaries by day, most-recent-first. Each
    (day, metric) is a single line under the day header."""
    items = [
        _summarize_bucket(
            date(2026, 5, 22), "resting_heart_rate",
            _PerMetricBucket(
                values=[61.0], units=["count/min"], row_count=1,
            ),
        ),
        _summarize_bucket(
            date(2026, 5, 22), "heart_rate_variability",
            _PerMetricBucket(
                values=[42.5], units=["ms"], row_count=1,
            ),
        ),
        _summarize_bucket(
            date(2026, 5, 21), "resting_heart_rate",
            _PerMetricBucket(
                values=[60.0], units=["count/min"], row_count=1,
            ),
        ),
    ]
    block = format_wearable_summary_block(items)
    assert "## Wearable summary" in block
    assert "2026-05-22" in block
    assert "2026-05-21" in block
    assert "resting_heart_rate" in block
    assert "heart_rate_variability" in block
    assert "avg=61.0" in block
    assert "avg=42.5" in block


def test_format_wearable_summary_block_empty():
    """Splice-friendly: empty input → empty string."""
    assert format_wearable_summary_block([]) == ""


def test_format_wearable_summary_block_includes_phrase_in_header():
    items = [
        _summarize_bucket(
            date(2026, 5, 22), "resting_heart_rate",
            _PerMetricBucket(
                values=[61.0], units=["count/min"], row_count=1,
            ),
        ),
    ]
    block = format_wearable_summary_block(items, phrase="last week")
    assert "(window: last week)" in block


def test_format_wearable_summary_sleep_with_no_durations_falls_back():
    """Sleep aggregation without ANY parseable duration must
    surface the gap honestly — segments=N (no durations) — not
    a fake 'avg=None' / 'total=None' leak."""
    items = [
        _summarize_bucket(
            date(2026, 5, 22), "sleep",
            _PerMetricBucket(row_count=8),  # no durations_min
        ),
    ]
    block = format_wearable_summary_block(items)
    assert "segments=8" in block
    assert "no durations" in block
    assert "None" not in block


def test_format_wearable_summary_render_units():
    items = [
        _summarize_bucket(
            date(2026, 5, 22), "active_energy",
            _PerMetricBucket(
                values=[540.0], units=["kcal"], row_count=1,
            ),
        ),
    ]
    block = format_wearable_summary_block(items)
    assert "total=540.0 kcal" in block


# ---------------------------------------------------------------------------
# Doctrine pins


# ---------------------------------------------------------------------------
# Sleep duration from timestamps (2026-05-22 evening hotfix —
# Apple HK sleep labels carry no duration string; the segment span
# date_end - date_start is the only reliable source).


def test_sleep_duration_renders_as_hours_minutes_when_durations_present():
    """Daily sleep total: sum of segment durations, formatted as
    'Xh Ym' instead of 'count=N' or '432 min'."""
    # 7 hours 12 minutes split across 4 segments.
    durations = [180.0, 120.0, 90.0, 42.0]  # 432 minutes total = 7h 12m
    b = _PerMetricBucket(durations_min=durations, row_count=4)
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    block = format_wearable_summary_block([s])
    assert "duration=7h 12m" in block
    # And the sample count is NOT shown for sleep (user-meaningless).
    assert "count=4" not in block
    assert "segments" not in block  # the no-duration fallback


def test_sleep_zero_duration_falls_back_to_segment_count():
    """If sleep rows have no parseable timestamps, surface the
    gap as 'segments=N (no durations)' instead of pretending to
    have data."""
    b = _PerMetricBucket(durations_min=[], row_count=5)
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    block = format_wearable_summary_block([s])
    assert "segments=5" in block
    assert "no durations" in block
    assert "duration=" not in block  # no fake duration emitted


def test_workout_duration_renders_in_hours_minutes_too():
    """Workout aggregation also gets the hours/minutes treatment
    via _format_hours_minutes, so 'duration=2h 11m' beats
    'duration=131 min' on a heavy day."""
    b = _PerMetricBucket(durations_min=[51.0, 30.0, 50.0], row_count=3)
    s = _summarize_bucket(date(2026, 5, 22), "workout", b)
    block = format_wearable_summary_block([s])
    assert "count=3" in block
    assert "duration=2h 11m" in block


def test_hours_minutes_formatter_edge_cases():
    """Pure-function sanity on the formatter helper."""
    from ownchart.retrieval.wearable_summary import _format_hours_minutes
    assert _format_hours_minutes(0) == "0m"
    assert _format_hours_minutes(45) == "45m"
    assert _format_hours_minutes(60) == "1h 0m"
    assert _format_hours_minutes(125) == "2h 5m"
    assert _format_hours_minutes(432.4) == "7h 12m"
    assert _format_hours_minutes(432.6) == "7h 13m"
    assert _format_hours_minutes(-5) == "0m"  # negative clamped


def test_summarize_window_pulls_date_end_in_select():
    """Static-source pin: the SELECT statement must include
    ExtractedFact.date_end so sleep + workout duration can be
    computed from timestamps (Apple HK sleep rows have no
    duration in the label)."""
    import inspect
    from ownchart.retrieval import wearable_summary as mod
    src = inspect.getsource(mod.summarize_wearable_window)
    assert "ExtractedFact.date_end" in src, (
        "summarize_wearable_window must include date_end in the "
        "SELECT — without it, sleep duration can't be computed."
    )
    # And the loop must actually use it.
    assert "dt_end" in src
    # FU-SLEEP-SEGMENT-DEDUP: timestamps become (start, end)
    # intervals that the summary step merges before summing.
    assert "b.intervals.append((dt, dt_end))" in src


def test_summarize_window_prefers_timestamp_intervals_over_label():
    """Static-source pin: when both date_end and label-parsed
    duration exist, the timestamp interval wins (more reliable
    AND mergeable). A label-only fallback covers Auto Export
    workout summary lines that don't have date_end."""
    import inspect
    from ownchart.retrieval import wearable_summary as mod
    src = inspect.getsource(mod.summarize_wearable_window)
    # Timestamp branch comes first.
    ts_idx = src.find("b.intervals.append((dt, dt_end))")
    assert ts_idx > 0, "interval append branch missing"
    # Label fallback comes after, inside an else.
    fallback_idx = src.find("parse_duration_minutes(label or", ts_idx)
    assert fallback_idx > ts_idx, (
        "label-parsed fallback must come AFTER the interval branch "
        "so timestamps win when available."
    )


# ---------------------------------------------------------------------------
# SpO2 display scaling (Apple HK 0.0–1.0 → 0–100%).


def test_oxygen_saturation_avg_scaled_to_percent():
    """SpO2 stored as 0.0–1.0 fraction; display as 0–100%.
    avg=0.972 → 'avg=97.2 %'."""
    b = _PerMetricBucket(
        values=[0.97, 0.98, 0.96],
        units=["count"],  # Apple may report unit as 'count' — irrelevant after scale
        row_count=3,
    )
    s = _summarize_bucket(date(2026, 5, 22), "oxygen_saturation", b)
    block = format_wearable_summary_block([s])
    # Decimal place is one; value scaled by 100.
    assert "avg=97.0 %" in block
    # The raw fractional form must NOT appear.
    assert "avg=0.97" not in block
    assert "avg=0.9 " not in block


def test_oxygen_saturation_min_max_also_scaled():
    """When min != max, the formatter emits a 'min=X, max=Y' line
    too. Both must be scaled to percent."""
    b = _PerMetricBucket(
        values=[0.92, 0.99],
        units=["count"],
        row_count=2,
    )
    s = _summarize_bucket(date(2026, 5, 22), "oxygen_saturation", b)
    block = format_wearable_summary_block([s])
    assert "min=92.0" in block
    assert "max=99.0" in block


def test_oxygen_saturation_unit_overridden_to_percent():
    """Even if the stored unit was "count" (Apple's quirky unit
    code), the display must show '%' for SpO2."""
    b = _PerMetricBucket(
        values=[0.97],
        units=["count"],
        row_count=1,
    )
    s = _summarize_bucket(date(2026, 5, 22), "oxygen_saturation", b)
    block = format_wearable_summary_block([s])
    assert "%" in block
    # The stored "count" unit must NOT leak into the display.
    assert "97.0 count" not in block


def test_non_spo2_metrics_unaffected_by_scaling():
    """Display scaling MUST be SpO2-specific (or any metric
    explicitly added to _DISPLAY_SCALE). HR, HRV, etc. must
    render as stored."""
    b = _PerMetricBucket(
        values=[58.2],
        units=["ms"],
        row_count=1,
    )
    s = _summarize_bucket(
        date(2026, 5, 22), "heart_rate_variability", b,
    )
    block = format_wearable_summary_block([s])
    assert "avg=58.2 ms" in block
    # No accidental scaling.
    assert "5820" not in block


def test_display_scale_map_only_includes_known_fraction_metrics():
    """Doctrine pin — _DISPLAY_SCALE must list only metrics
    whose storage form differs from human-readable form. Today
    that's oxygen_saturation; future additions need explicit
    justification (otherwise rendering drifts silently)."""
    from ownchart.retrieval.wearable_summary import _DISPLAY_SCALE
    # SpO2 is the only entry; factor 100, unit "%".
    assert _DISPLAY_SCALE == {"oxygen_saturation": (100.0, "%")}


# ---------------------------------------------------------------------------
# PM-failing question end-to-end shape (no DB — pure function chain).


def test_format_block_for_pm_failing_question_renders_sleep_hours():
    """Synthetic input mirrors what the PM-failing live query
    would render under v2 (no duration) vs the fix. Confirms the
    user-facing line transitions from 'count=N' to 'duration=Xh Ym'."""
    items = [
        _summarize_bucket(
            date(2026, 5, 22),
            "sleep",
            _PerMetricBucket(
                durations_min=[420.0],  # 7h 0m
                row_count=8,
            ),
        ),
        _summarize_bucket(
            date(2026, 5, 22),
            "oxygen_saturation",
            _PerMetricBucket(
                values=[0.97],
                units=["count"],
                row_count=1,
            ),
        ),
    ]
    block = format_wearable_summary_block(items)
    # Sleep: hours/minutes, NOT raw minute count
    assert "duration=7h 0m" in block
    # SpO2: percent, NOT fraction
    assert "%" in block
    assert "avg=0.9" not in block
    # And no leaking "count=" for sleep (which was the v2 bug)
    assert "sleep: count=8" not in block


# ---------------------------------------------------------------------------
# FU-SLEEP-SEGMENT-DEDUP (2026-05-22): interval union-merge.
#
# Apple HK writes sleep nights from multiple sources (Apple Watch +
# iPhone Health + third-party apps) AND splits each night into
# InBed vs AsleepCore/REM/Deep stage rows. Summing raw durations
# multiplies the real span (live test surfaced 220h sleep/day).
#
# Interval-merge collapses overlapping or touching intervals so
# the same wall-clock window only counts once.


_T = datetime  # alias for brevity in fixtures
_TZ = timezone.utc


def _i(start_hr: float, end_hr: float, day: date | None = None) -> tuple[_T, _T]:
    """Build an interval on a fixed test date for readability.
    Hours can be fractional (e.g. 1.5 = 1:30). Uses 2026-05-20."""
    day = day or date(2026, 5, 20)
    def _hr_to_dt(hr: float) -> _T:
        h = int(hr)
        m = int(round((hr - h) * 60))
        return _T(day.year, day.month, day.day, h, m, tzinfo=_TZ)
    return _hr_to_dt(start_hr), _hr_to_dt(end_hr)


# Empty / single-interval baselines


def test_merge_empty_list_returns_zero():
    assert _merge_intervals_minutes([]) == 0.0


def test_merge_single_interval_returns_its_length():
    iv = _i(0.0, 1.0)  # 1 hour
    assert _merge_intervals_minutes([iv]) == pytest.approx(60.0)


# PM-required: identical intervals count once


def test_identical_intervals_collapse_to_one():
    """Same interval reported by 2 sources must not double-count."""
    a = _i(0.0, 8.0)  # 12am-8am
    b = _i(0.0, 8.0)  # exact duplicate
    assert _merge_intervals_minutes([a, b]) == pytest.approx(480.0)


def test_three_identical_intervals_still_one_span():
    iv = _i(1.0, 2.0)
    assert _merge_intervals_minutes([iv, iv, iv]) == pytest.approx(60.0)


# PM-required: overlapping intervals merge


def test_overlapping_intervals_merge_to_union_span():
    """12am-3am + 2am-5am → union 12am-5am = 5h, not 6h."""
    a = _i(0.0, 3.0)  # 3h
    b = _i(2.0, 5.0)  # 3h, overlaps the last hour
    out = _merge_intervals_minutes([a, b])
    assert out == pytest.approx(300.0)  # 5h


def test_partially_overlapping_three_segments():
    """0-2, 1-3, 2-4 → union 0-4 = 4h, not 6h."""
    out = _merge_intervals_minutes([_i(0.0, 2.0), _i(1.0, 3.0), _i(2.0, 4.0)])
    assert out == pytest.approx(240.0)


def test_touching_intervals_merge():
    """End-to-end touching intervals (1:00-2:00, 2:00-3:00) treat
    as one span (2h). Apple HK occasionally splits a window at
    the second boundary."""
    out = _merge_intervals_minutes([_i(1.0, 2.0), _i(2.0, 3.0)])
    assert out == pytest.approx(120.0)


# PM-required: non-overlapping intervals sum


def test_non_overlapping_intervals_sum():
    """Separate naps don't merge."""
    out = _merge_intervals_minutes([_i(1.0, 2.0), _i(4.0, 5.0)])
    assert out == pytest.approx(120.0)  # 2h total, two distinct hours


def test_unsorted_input_is_sorted_internally():
    """Caller can pass intervals in any order; merge sorts by
    start time before sweeping."""
    out = _merge_intervals_minutes([_i(4.0, 5.0), _i(0.0, 2.0), _i(1.0, 3.0)])
    # Union = 0-3 + 4-5 = 4h
    assert out == pytest.approx(240.0)


# PM-required: synthetic multi-source/stage night still yields
# the real sleep span, not a multiplied duration.


def test_realistic_sleep_night_with_4_sources_x_4_stages():
    """Synthetic night that mirrors the live bug:
       - Apple Watch writes InBed + AsleepCore + AsleepREM + AsleepDeep
       - iPhone Health writes the same 4 stages
       - AutoSleep writes one full-night InBed
       - SleepCycle writes one full-night InBed
       Total = ~10 rows × full overlap. Real night = 8h (0am-8am).
    """
    full = _i(0.0, 8.0)
    core1 = _i(0.0, 2.0)
    rem1 = _i(2.0, 3.0)
    deep1 = _i(3.0, 4.5)
    core2 = _i(4.5, 8.0)
    intervals = [
        # Apple Watch
        full, core1, rem1, deep1, core2,
        # iPhone Health (identical breakdown)
        full, core1, rem1, deep1, core2,
        # AutoSleep + SleepCycle (full-night)
        full, full,
    ]
    out = _merge_intervals_minutes(intervals)
    assert out == pytest.approx(480.0)  # exactly 8h, not 8h × multiplier


# PM-required: plausible single-source case remains unchanged.


def test_single_source_segmented_night_sums_to_actual_span():
    """One source breaks the night into 5 non-overlapping stages
    that tile the full 7.5h window. Merged span = 7.5h."""
    intervals = [
        _i(0.0, 1.5),   # AsleepCore
        _i(1.5, 2.5),   # REM
        _i(2.5, 4.0),   # Deep
        _i(4.0, 5.0),   # REM
        _i(5.0, 7.5),   # Core
    ]
    out = _merge_intervals_minutes(intervals)
    assert out == pytest.approx(450.0)  # 7.5h


def test_single_full_night_interval_unchanged():
    """Simplest case — one row, one interval. Merge passes
    through unchanged."""
    out = _merge_intervals_minutes([_i(22.0, 23.0)])
    assert out == pytest.approx(60.0)


# End-to-end through _summarize_bucket + formatter


def test_summarize_bucket_consumes_intervals_for_sleep():
    """_summarize_bucket combines bucket.intervals (merged) +
    bucket.durations_min (label-parsed sum) into duration_min_sum."""
    iv1 = _i(0.0, 8.0)
    iv2 = _i(0.0, 8.0)  # duplicate, must dedupe
    b = _PerMetricBucket(intervals=[iv1, iv2], row_count=2)
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    # Merged = 8h = 480m
    assert s.duration_min_sum == pytest.approx(480.0)


def test_summarize_bucket_combines_intervals_and_label_durations():
    """When BOTH intervals and label durations are present (the
    cross-adapter case — native HK intervals + Auto Export label
    minutes), the total = merged_intervals_minutes + sum(label)."""
    iv = _i(0.0, 1.0)  # 60 min via timestamp
    b = _PerMetricBucket(
        intervals=[iv],
        durations_min=[15.0],  # 15-min walk from Auto Export label
        row_count=2,
    )
    s = _summarize_bucket(date(2026, 5, 22), "workout", b)
    assert s.duration_min_sum == pytest.approx(75.0)


def test_format_block_renders_deduped_sleep_total_as_hours_minutes():
    """End-to-end: PM-failing live shape — 220h "sleep" caused by
    overlap stacking. After dedupe, a synthetic 8h night with
    10× overlap should render as 'duration=8h 0m', not '80h 0m'."""
    full = _i(0.0, 8.0)
    intervals = [full] * 10  # 10 copies — 4 sources × 2.5 stage avg
    b = _PerMetricBucket(intervals=intervals, row_count=10)
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    block = format_wearable_summary_block([s])
    assert "duration=8h 0m" in block
    # The pre-dedup multiplied form must NOT appear.
    assert "duration=80h" not in block


def test_workout_overlap_merged_too():
    """PM scope: same interval-merge applies to workout. If
    Strava and Apple Watch both log the same 6am-7am run, merge
    to 1h not 2h."""
    strava = _i(6.0, 7.0)
    watch = _i(6.0 + 1/60.0, 6.0 + 58/60.0)  # 6:01-6:58
    b = _PerMetricBucket(intervals=[strava, watch], row_count=2)
    s = _summarize_bucket(date(2026, 5, 22), "workout", b)
    # Merged = 6:00-7:00 = 60 min (strava envelopes watch).
    assert s.duration_min_sum == pytest.approx(60.0)


def test_non_overlapping_workouts_sum_normally():
    """Two distinct workouts on the same day shouldn't dedupe."""
    morning = _i(6.0, 7.0)
    evening = _i(17.0, 18.0)
    b = _PerMetricBucket(intervals=[morning, evening], row_count=2)
    s = _summarize_bucket(date(2026, 5, 22), "workout", b)
    assert s.duration_min_sum == pytest.approx(120.0)


def test_summarize_bucket_no_intervals_no_durations_stays_none():
    """Bucket with no timestamps and no label durations →
    duration_min_sum stays None (renderer falls back to sample
    count). Defensive — confirms we don't accidentally emit 0.0
    as a fake duration."""
    b = _PerMetricBucket(row_count=3)
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    assert s.duration_min_sum is None


# Static-source pin on the loop


def test_summarize_window_appends_intervals_not_durations_for_sleep_workout():
    """Static-source check: when both date_start and date_end are
    present for a sleep/workout row, the loop must append to
    b.intervals (so the dedupe step can merge), NOT to
    b.durations_min (which is summed unchanged)."""
    import inspect
    from ownchart.retrieval import wearable_summary as mod
    src = inspect.getsource(mod.summarize_wearable_window)
    assert "b.intervals.append((dt, dt_end))" in src, (
        "summarize_wearable_window must collect intervals (not "
        "pre-summed durations) for sleep/workout when timestamps "
        "exist — otherwise the dedupe in _summarize_bucket can't "
        "merge them."
    )


def test_no_phi_constants_in_module():
    """Defensive — the wearable_summary module ships test patterns
    + format templates. Spot-check that there's no embedded PHI
    (provider names, patient identifiers, real DOB-shaped values,
    etc.) in the module source."""
    import inspect
    from ownchart.retrieval import wearable_summary as mod
    src = inspect.getsource(mod)
    forbidden = (
        "Nick", "Dawson", "Bozeman", "Stanford", "OrthoVirginia",
        "UVA Health", "Buckland", "Hansen", "Hutchens",
        "JUNCTIONAL", "NEVUS", "MELAN-A", "PRAME",
    )
    for term in forbidden:
        assert term not in src, (
            f"wearable_summary module contains forbidden term {term!r}"
        )
