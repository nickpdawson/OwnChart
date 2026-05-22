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


def test_aggregation_sleep_uses_count_with_duration():
    b = _PerMetricBucket(
        values=[], durations_min=[420.0], row_count=4,
    )
    s = _summarize_bucket(date(2026, 5, 22), "sleep", b)
    assert s.aggregation == "count_with_duration"
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
        ("sleep", "count_with_duration"),
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


def test_format_wearable_summary_count_only_metrics():
    """Sleep aggregation without parseable duration → count only;
    no fake 'avg=None' / 'total=None' leak."""
    items = [
        _summarize_bucket(
            date(2026, 5, 22), "sleep",
            _PerMetricBucket(row_count=8),
        ),
    ]
    block = format_wearable_summary_block(items)
    assert "count=8" in block
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
