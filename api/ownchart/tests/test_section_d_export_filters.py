"""Section D Phase 1 — Export filter resolver.

Pure-function tests for `resolve_filters` and the request-shape
contract. No DB.

What this pins:
  - None (pre-Section-D job or unfiltered request) → full record:
    no date bounds, all three domains included.
  - 'all' kind → no date bounds.
  - 'last_90d' / 'last_1y' → date_start computed from `now`.
  - 'custom' with valid start → honored; end defaults to `now`.
  - 'custom' with swapped start/end → resolver swaps so we don't
    silently return zero rows.
  - Invalid date_range_kind in stored JSONB → collapses to 'all'
    (defensive against hand-written / corrupted job rows).
  - Domain list empty / missing → defaults to all three.
  - fact_method_is_body_signal classifier pins which extraction
    methods count as body signals.
  - CreateExportRequest defaults to "no filter" → ExportJob.filters
    is still set (so the runner reads a stable shape) but resolves
    to full-record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ownchart.exports.snapshot import (
    ResolvedFilters,
    fact_method_is_body_signal,
    resolve_filters,
)
from ownchart.routes.exports import (
    CreateExportRequest,
    ExportFilters,
    _filters_to_jsonb,
)


# ---------------------------------------------------------------------------
# resolve_filters


def test_none_filters_means_full_record():
    """The pre-Section-D state. Runner receives `filters=None` and
    pulls every collection without bounds."""
    out = resolve_filters(None)
    assert out == ResolvedFilters(
        date_start=None,
        date_end=None,
        include_clinical=True,
        include_body_signals=True,
        include_calendar=True,
    )


def test_empty_dict_filters_treated_as_defaults():
    """Defensive: a corrupted/empty JSONB shouldn't crash the runner.
    Resolver returns the same full-record default."""
    out = resolve_filters({})
    assert out.include_clinical is True
    assert out.include_body_signals is True
    assert out.include_calendar is True
    assert out.date_start is None
    assert out.date_end is None


def test_all_kind_no_bounds():
    out = resolve_filters({"date_range_kind": "all"})
    assert out.date_start is None
    assert out.date_end is None


def test_last_90d_window_is_90_days_back():
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    out = resolve_filters({"date_range_kind": "last_90d"}, now=now)
    assert out.date_end is None  # we don't cap the upper end
    assert out.date_start is not None
    delta = now - out.date_start
    # Days within 89.5–90.5 is the tolerance for the floor(timedelta) edge.
    assert 89 <= delta.days <= 90


def test_last_1y_window_is_365_days_back():
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    out = resolve_filters({"date_range_kind": "last_1y"}, now=now)
    assert out.date_start == now - timedelta(days=365)


def test_custom_with_start_only_defaults_end_to_now():
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    out = resolve_filters(
        {
            "date_range_kind": "custom",
            "date_range_start": "2020-01-01T00:00:00Z",
        },
        now=now,
    )
    assert out.date_start == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert out.date_end == now


def test_custom_with_start_and_end_honors_both():
    out = resolve_filters({
        "date_range_kind": "custom",
        "date_range_start": "2020-01-01T00:00:00Z",
        "date_range_end":   "2022-12-31T00:00:00Z",
    })
    assert out.date_start == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert out.date_end == datetime(2022, 12, 31, tzinfo=timezone.utc)


def test_custom_with_swapped_dates_normalizes():
    """If start > end, the resolver swaps them so the snapshot
    doesn't return zero rows for a fat-fingered request."""
    out = resolve_filters({
        "date_range_kind": "custom",
        "date_range_start": "2022-12-31T00:00:00Z",
        "date_range_end":   "2020-01-01T00:00:00Z",
    })
    assert out.date_start == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert out.date_end == datetime(2022, 12, 31, tzinfo=timezone.utc)


def test_invalid_kind_collapses_to_all():
    """Pydantic on the request side already rejects bad values; this
    pins defensive behavior against a corrupted JSONB row, since the
    runner reads the filter envelope back out of the DB."""
    out = resolve_filters({"date_range_kind": "since-the-dawn-of-time"})
    assert out.date_start is None
    assert out.date_end is None


def test_naive_iso_input_treated_as_utc():
    """ISO timestamps without a timezone are read as UTC so
    downstream `>=` / `<=` comparisons against tz-aware DB columns
    don't raise."""
    out = resolve_filters({
        "date_range_kind": "custom",
        "date_range_start": "2020-01-01T00:00:00",
    })
    assert out.date_start is not None
    assert out.date_start.tzinfo is not None


def test_malformed_iso_string_silently_ignored():
    """A non-parseable string yields None for that bound; the rest of
    the envelope still applies."""
    out = resolve_filters({
        "date_range_kind": "custom",
        "date_range_start": "not-a-date",
        "date_range_end":   "2022-12-31T00:00:00Z",
    })
    assert out.date_start is None
    assert out.date_end == datetime(2022, 12, 31, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Domain filter


def test_clinical_only_excludes_body_signals_and_calendar():
    out = resolve_filters({"domains": ["clinical"]})
    assert out.include_clinical is True
    assert out.include_body_signals is False
    assert out.include_calendar is False


def test_body_signals_only_excludes_clinical_and_calendar():
    out = resolve_filters({"domains": ["body_signals"]})
    assert out.include_body_signals is True
    assert out.include_clinical is False
    assert out.include_calendar is False


def test_calendar_only():
    out = resolve_filters({"domains": ["calendar"]})
    assert out.include_calendar is True
    assert out.include_clinical is False
    assert out.include_body_signals is False


def test_empty_domains_list_defaults_to_all_three():
    """Conservative: an empty domain list shouldn't mean "no data."
    Defaults to the full set so the UI never silently exports nothing."""
    out = resolve_filters({"domains": []})
    assert out.include_clinical is True
    assert out.include_body_signals is True
    assert out.include_calendar is True


def test_unknown_domain_silently_ignored():
    """A future-domain string we don't recognize doesn't crash."""
    out = resolve_filters({"domains": ["clinical", "ai_summaries"]})
    assert out.include_clinical is True
    # ai_summaries is the "coming soon" placeholder; backend doesn't
    # surface conversations into the snapshot yet, so an explicit
    # request for it has no effect today.
    assert out.include_body_signals is False
    assert out.include_calendar is False


# ---------------------------------------------------------------------------
# Body-signal classifier


def test_body_signal_classifier_includes_auto_export():
    assert fact_method_is_body_signal("health_auto_export") is True


def test_body_signal_classifier_includes_native_healthkit():
    assert fact_method_is_body_signal("native_healthkit") is True


def test_body_signal_classifier_excludes_clinical_methods():
    for method in (
        "fhir_resource",
        "ccda_xpath",
        "claude_vision_v1",
        "ocr_tesseract",
        "patient_self_report",
    ):
        assert fact_method_is_body_signal(method) is False


def test_body_signal_classifier_handles_none_and_unknown():
    assert fact_method_is_body_signal(None) is False
    assert fact_method_is_body_signal("") is False
    assert fact_method_is_body_signal("brand_new_method") is False


# ---------------------------------------------------------------------------
# Request shape contract


def test_default_request_yields_full_record_filters():
    """User who POSTs `{}` to /api/exports gets the full-record
    default — no filters, no domain restriction."""
    req = CreateExportRequest()
    assert req.requested_format == "all"
    assert req.filters.date_range_kind == "all"
    assert req.filters.domains == ["clinical", "body_signals", "calendar"]


def test_filters_to_jsonb_round_trips_through_resolver():
    """End-to-end pin: the JSONB shape persisted on ExportJob is the
    SAME shape `resolve_filters` accepts. Catches drift between the
    route's `_filters_to_jsonb` writer and the runner's reader."""
    req_filters = ExportFilters(
        date_range_kind="custom",
        date_range_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        date_range_end=datetime(2022, 12, 31, tzinfo=timezone.utc),
        domains=["clinical", "calendar"],
    )
    jsonb = _filters_to_jsonb(req_filters)
    resolved = resolve_filters(jsonb)
    assert resolved.date_start == datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert resolved.date_end == datetime(2022, 12, 31, tzinfo=timezone.utc)
    assert resolved.include_clinical is True
    assert resolved.include_body_signals is False
    assert resolved.include_calendar is True


def test_filters_to_jsonb_emits_iso_strings_not_datetimes():
    """JSONB column doesn't natively serialize datetime objects; the
    `_filters_to_jsonb` helper has to convert. Pinning so a future
    refactor doesn't regress to writing tz-aware datetimes that
    psycopg can't bind."""
    req_filters = ExportFilters(
        date_range_kind="custom",
        date_range_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        date_range_end=datetime(2022, 12, 31, tzinfo=timezone.utc),
    )
    jsonb = _filters_to_jsonb(req_filters)
    assert isinstance(jsonb["date_range_start"], str)
    assert isinstance(jsonb["date_range_end"], str)
    assert jsonb["date_range_start"].startswith("2020-01-01")


def test_filters_to_jsonb_none_bounds_stay_none():
    req_filters = ExportFilters()  # all defaults
    jsonb = _filters_to_jsonb(req_filters)
    assert jsonb["date_range_kind"] == "all"
    assert jsonb["date_range_start"] is None
    assert jsonb["date_range_end"] is None
    assert jsonb["domains"] == ["clinical", "body_signals", "calendar"]
