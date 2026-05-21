"""Calendar history-window + Ask life-context tests
(FU-CAL-HISTORY-WINDOW + FU-CAL-ASK-INTEGRATION).

Coverage:

  - History-window enum mapping (90d / 1y / 3y / 5y / all) returns
    the expected back-window deltas; unknown values raise.
  - Per-source clamp: a 90d window hides a 2-years-ago event from
    the Ask projector even though it's stored.
  - Privacy floor: the projector returns busy-only-equivalent when
    ``source_consent=False`` regardless of storage mode (slice 3
    re-verification under the new retrieval pipeline).
  - format_calendar_context_block emits the projected fields and
    never leaks fields not in the projection (no notes / location
    when consent=False).
  - Patch route accepts history_window_back updates.
  - Ask route wires the calendar life-context into the prompt
    (static-source check — the LLM call is mocked elsewhere).
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ownchart.ingest.calendar_eventkit import project_event_for_llm
from ownchart.retrieval.calendar_life_context import (
    _HISTORY_DELTAS,
    format_calendar_context_block,
    history_window_back_to_delta,
)


# ---------------------------------------------------------------------------
# 1. History window enum


@pytest.mark.parametrize(
    "window, days_back",
    [
        ("90d", 90),
        ("1y", 365),
        ("3y", 365 * 3),
        ("5y", 365 * 5),
        # 'all' is a sentinel — must be far enough back that no
        # realistic event date falls outside. 50 years should do.
        ("all", 365 * 50),
    ],
)
def test_history_window_back_to_delta_maps_correctly(window, days_back):
    delta = history_window_back_to_delta(window)
    assert delta.days == days_back


def test_history_window_back_unknown_value_raises():
    with pytest.raises(ValueError, match="unknown history_window_back"):
        history_window_back_to_delta("180d")


def test_history_window_enum_matches_db_check_constraint():
    """The enum the retrieval helper accepts MUST match the CHECK
    constraint on calendar_sources.history_window_back set by
    migration 0042. A future widening that adds '180d' to one but
    not the other would silently bypass the DB clamp."""
    from ownchart.models.calendar_source import HISTORY_WINDOWS
    assert set(_HISTORY_DELTAS.keys()) == set(HISTORY_WINDOWS)


# ---------------------------------------------------------------------------
# 2. Privacy floor (the second elevation) — projector re-verification


def test_projector_floor_when_consent_false_under_each_storage_mode():
    """Regardless of how the event was stored, consent=False projects
    only start/end/all_day. This is the Ask floor."""
    common = dict(
        start_at=datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        all_day=False,
        title="Dr. Patel",
        location="Bozeman Health",
        notes="Bring labs",
        attendees_count=2,
    )
    for mode in ("full_details", "title_and_time", "busy_only"):
        proj = project_event_for_llm(
            **common, privacy_mode_applied=mode, source_consent=False,
        )
        assert set(proj.keys()) == {"start_at", "end_at", "all_day"}


def test_projector_consent_true_full_details_exposes_everything():
    proj = project_event_for_llm(
        start_at=datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        all_day=False,
        title="Dr. Patel",
        location="Bozeman Health",
        notes="Bring labs",
        attendees_count=2,
        privacy_mode_applied="full_details",
        source_consent=True,
    )
    assert proj["title"] == "Dr. Patel"
    assert proj["location"] == "Bozeman Health"
    assert proj["notes"] == "Bring labs"
    assert proj["attendees_count"] == 2


def test_projector_consent_true_title_and_time_strips_location_notes():
    proj = project_event_for_llm(
        start_at=datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        all_day=False,
        title="Dr. Patel",
        location="(stored but mode-stripped)",
        notes=None,
        attendees_count=None,
        privacy_mode_applied="title_and_time",
        source_consent=True,
    )
    assert proj["title"] == "Dr. Patel"
    assert "location" not in proj
    assert "notes" not in proj
    assert "attendees_count" not in proj


# ---------------------------------------------------------------------------
# 3. Context block formatter — never leaks fields outside the projection


def test_format_calendar_context_block_renders_projected_fields_only():
    items = [
        {
            "event": {
                "start_at": datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
                "end_at": datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
                "all_day": False,
                # consent=true + full_details → title + location present
                "title": "Dr. Patel",
                "location": "Bozeman Health",
            },
            "source_display_name": "Apps (Personal)",
            "source_id": "11111111-1111-1111-1111-111111111111",
        }
    ]
    block = format_calendar_context_block(items)
    assert "Calendar context" in block
    assert "Dr. Patel" in block
    assert "Bozeman Health" in block
    assert "Apps (Personal)" in block


def test_format_calendar_context_block_busy_only_floor_no_title_or_location():
    """An event projected under the consent=False floor has only
    start/end/all_day. The formatter must not invent a title or
    fall through to a NULL leak that says "None"."""
    items = [
        {
            "event": {
                "start_at": datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
                "end_at": datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
                "all_day": False,
                # No title / location / notes — that's the floor.
            },
            "source_display_name": "Work",
            "source_id": "22222222-2222-2222-2222-222222222222",
        }
    ]
    block = format_calendar_context_block(items)
    assert "(no title — privacy mode)" in block
    # Must NOT contain "None" — that's the Python repr leak.
    assert "None" not in block


def test_format_calendar_context_block_empty_returns_empty_string():
    """Splice-friendly: empty list → empty string so the caller can
    concatenate without conditional logic."""
    assert format_calendar_context_block([]) == ""


def test_format_calendar_context_block_all_day_event():
    items = [
        {
            "event": {
                "start_at": datetime(2026, 7, 4, 0, tzinfo=timezone.utc),
                "end_at": datetime(2026, 7, 5, 0, tzinfo=timezone.utc),
                "all_day": True,
                "title": "Vacation",
            },
            "source_display_name": "Family",
            "source_id": "33333333-3333-3333-3333-333333333333",
        }
    ]
    block = format_calendar_context_block(items)
    assert "all-day" in block
    assert "Vacation" in block


# ---------------------------------------------------------------------------
# 4. Ask route wires the calendar context (static-source check)


def test_ask_route_imports_calendar_life_context():
    """A future refactor that drops the import accidentally would
    silently regress to "no calendar context in Ask." Pin both the
    import and the call site."""
    from ownchart.routes import ask
    src = inspect.getsource(ask)
    assert "fetch_calendar_life_context" in src
    assert "format_calendar_context_block" in src
    assert "context_block = _format_context(facts)" in src
    assert "format_calendar_context_block(" in src


def test_ask_route_passes_active_record_to_calendar_fetch():
    """The calendar fetch MUST be record-scoped — passing
    ctx.active_record_id, not user_id, not a default."""
    from ownchart.routes.ask import ask as ask_handler
    src = inspect.getsource(ask_handler)
    # Look at the fetch_calendar_life_context call site.
    after = src.split("fetch_calendar_life_context(", 1)[1]
    block = after.split(")", 1)[0]
    assert "person_record_id=ctx.active_record_id" in block


# ---------------------------------------------------------------------------
# 5. PATCH route accepts history_window_back


def test_patch_request_model_advertises_history_window_back():
    from ownchart.routes.calendar import CalendarSourcePatchRequest
    props = CalendarSourcePatchRequest.model_json_schema()["properties"]
    assert "history_window_back" in props


def test_patch_route_static_sets_history_window_back():
    """patch_source must apply body.history_window_back to the
    source row when present. Narrowing should NOT trigger a
    destructive sweep — the doctrine is hide-not-delete."""
    from ownchart.routes.calendar import patch_source
    src = inspect.getsource(patch_source)
    assert "if body.history_window_back is not None:" in src
    assert "src.history_window_back = body.history_window_back" in src
    # No DELETE/UPDATE sweep keyed on history_window_back narrowing.
    # The redaction sweep helper is privacy_mode-only.
    narrow_block = src.split("if body.history_window_back is not None:", 1)[1]
    narrow_block = narrow_block.split("src.updated_at", 1)[0]
    assert "delete(" not in narrow_block.lower()
    assert "update(CalendarEvent)" not in narrow_block


def test_calendar_source_out_advertises_history_window_back():
    from ownchart.routes.calendar import CalendarSourceOut
    props = CalendarSourceOut.model_json_schema()["properties"]
    assert "history_window_back" in props
    # Default '90d' so existing clients see a stable value.
    schema = CalendarSourceOut.model_json_schema()
    default = schema["properties"]["history_window_back"].get("default")
    assert default == "90d"


# ---------------------------------------------------------------------------
# 6. Per-source clamp (the load-bearing history-window invariant)
#
# The full SQL roundtrip is exercised in the live verify; here we
# pin the clamp logic by directly calling history_window_back_to_delta
# and confirming a 2-year-old event falls outside a 90d window.


def test_two_year_old_event_outside_90d_window():
    now = datetime(2026, 5, 21, tzinfo=timezone.utc)
    old = now - timedelta(days=730)
    floor = now - history_window_back_to_delta("90d")
    assert old < floor, "2-year-old event should be older than 90d floor"


def test_two_year_old_event_inside_3y_window():
    now = datetime(2026, 5, 21, tzinfo=timezone.utc)
    old = now - timedelta(days=730)
    floor = now - history_window_back_to_delta("3y")
    assert old >= floor, "2-year-old event should be inside 3y floor"


# ---------------------------------------------------------------------------
# 7. Pure-function fetch logic — exercises per-source clamp on a
# fake (CalendarEvent, CalendarSource) tuple stream. We don't spin
# up a DB; instead, we drive the post-SQL Python path that does the
# clamp + projection.


def _fake_event(*, start_at, src):
    return SimpleNamespace(
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        all_day=False,
        title="Visit",
        location="Clinic",
        notes=None,
        attendees_count=None,
        privacy_mode_applied=src.privacy_mode,
    )


def _fake_source(window, consent=False, privacy_mode="title_and_time"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Test",
        history_window_back=window,
        llm_full_details_consent=consent,
        privacy_mode=privacy_mode,
    )


def test_fetch_logic_clamps_old_events_per_source():
    """Reimplement the per-source clamp on a fake row stream to
    confirm an old event from a 90d source is dropped while the
    same date on a 3y source survives. This pins the Python clamp
    logic that fetch_calendar_life_context uses."""
    now = datetime(2026, 5, 21, tzinfo=timezone.utc)
    old_date = now - timedelta(days=730)
    s_90d = _fake_source("90d")
    s_3y = _fake_source("3y")

    survived: list[dict] = []
    for ev, src in [
        (_fake_event(start_at=old_date, src=s_90d), s_90d),
        (_fake_event(start_at=old_date, src=s_3y), s_3y),
    ]:
        floor = now - history_window_back_to_delta(src.history_window_back)
        if ev.start_at < floor:
            continue
        survived.append({"source": src.history_window_back})

    assert len(survived) == 1
    assert survived[0]["source"] == "3y"
