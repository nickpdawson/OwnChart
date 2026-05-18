"""M02 Slice 3 — EventKit calendar ingest tests.

Pure-function coverage of the four privacy / lifecycle contracts the
slice enforces. No DB, no LLM, no TestClient. The DB-side and
request-flow checks for the perimeter are static-source inspections
(same pattern as `test_perimeter_external_ingest.py`).

Per PM directive, four contracts must be pinned:

  1. **Record scoping.** Routes must filter by ``ctx.active_record_id``
     on every read AND stamp it on every write. Cross-record probes
     return 404, not 403.

  2. **Privacy-mode redaction at ingest.** Defense in depth: server
     re-applies the source's privacy_mode regardless of what iOS
     sent. A chatty title that crosses the wire under busy_only
     is silently dropped before insert.

  3. **30-day tombstones.** Soft-delete is the user-facing disconnect
     signal; hard delete happens after the TTL.

  4. **LLM exposure floor.** ``project_event_for_llm`` returns
     busy_only-equivalent regardless of stored fields when
     ``source_consent`` is false. The second elevation
     (``llm_full_details_consent``) is what unlocks visibility —
     storage alone is not enough.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ownchart.ingest.calendar_eventkit import (
    IOSEventKitEvent,
    PRIVACY_MODES_TUPLE,
    PURGE_TTL_DAYS_DEFAULT,
    compute_purge_cutoff,
    project_event_for_llm,
    redact_event_for_storage,
)
from ownchart.models import CalendarEvent, CalendarSource


# ---------------------------------------------------------------------------
# Helpers


def _event_dict(**over) -> dict:
    base = {
        "external_id": "ek-event-1",
        "external_modified_at": datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        "start_at": datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        "end_at": datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
        "all_day": False,
        "title": "Dr. Patel — annual physical",
        "location": "Bozeman Health, Suite 305",
        "notes": "Bring last lab panel + medication list.",
        "attendees_count": 2,
        "metadata": {"recurrence": None, "ek_color": "#FF9933"},
        "tombstoned": False,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Privacy-mode redaction at ingest (defense in depth)


def test_busy_only_zeros_all_four_user_visible_fields():
    out = redact_event_for_storage(_event_dict(), privacy_mode="busy_only")
    assert out["title"] is None
    assert out["location"] is None
    assert out["notes"] is None
    assert out["attendees_count"] is None


def test_busy_only_preserves_times_and_all_day():
    """The row's existence is the 'busy' signal — start/end MUST be
    present regardless of privacy mode."""
    out = redact_event_for_storage(
        _event_dict(all_day=True), privacy_mode="busy_only",
    )
    assert out["start_at"] == datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    assert out["end_at"] == datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)
    assert out["all_day"] is True


def test_title_and_time_keeps_title_strips_others():
    out = redact_event_for_storage(
        _event_dict(), privacy_mode="title_and_time",
    )
    assert out["title"] == "Dr. Patel — annual physical"
    assert out["location"] is None
    assert out["notes"] is None
    assert out["attendees_count"] is None


def test_full_details_keeps_all_four_fields():
    out = redact_event_for_storage(_event_dict(), privacy_mode="full_details")
    assert out["title"] == "Dr. Patel — annual physical"
    assert out["location"] == "Bozeman Health, Suite 305"
    assert out["notes"] == "Bring last lab panel + medication list."
    assert out["attendees_count"] == 2


def test_privacy_mode_stamped_on_redacted_output():
    """The DB row's ``privacy_mode_applied`` is what a future
    tightening sweep keys off. Must reflect the mode used at this
    ingest."""
    for mode in PRIVACY_MODES_TUPLE:
        out = redact_event_for_storage(_event_dict(), privacy_mode=mode)
        assert out["privacy_mode_applied"] == mode


def test_raw_metadata_preserved_across_all_privacy_modes():
    """Metadata is provenance (recurrence pattern, EK color), not user
    content. Slice 3 contract: preserved across modes. iOS smuggling
    text into metadata would be an iOS bug — server can't validate
    every key."""
    md = {"recurrence": "weekly_tuesday", "ek_color": "#0066CC"}
    for mode in PRIVACY_MODES_TUPLE:
        out = redact_event_for_storage(
            _event_dict(metadata=md), privacy_mode=mode,
        )
        assert out["raw_metadata"] == md


def test_redact_accepts_dict_or_pydantic_model():
    ev_dict = _event_dict()
    ev_model = IOSEventKitEvent.model_validate(ev_dict)
    out_dict = redact_event_for_storage(ev_dict, privacy_mode="full_details")
    out_model = redact_event_for_storage(ev_model, privacy_mode="full_details")
    assert out_dict == out_model


def test_redact_rejects_unknown_privacy_mode():
    with pytest.raises(ValueError, match="unknown privacy_mode"):
        redact_event_for_storage(_event_dict(), privacy_mode="weird_mode")  # type: ignore[arg-type]


def test_redact_carries_tombstoned_flag_for_route_dispatch():
    """The route layer reads ``tombstoned`` to choose between UPSERT
    and tombstone UPDATE. The redactor passes it through."""
    out = redact_event_for_storage(
        _event_dict(tombstoned=True), privacy_mode="full_details",
    )
    assert out["tombstoned"] is True


def test_iosevent_rejects_negative_attendees_count():
    with pytest.raises(ValidationError):
        IOSEventKitEvent.model_validate(_event_dict(attendees_count=-1))


def test_iosevent_rejects_oversized_title():
    with pytest.raises(ValidationError):
        IOSEventKitEvent.model_validate(_event_dict(title="x" * 600))


def test_iosevent_optional_fields_omit_cleanly():
    """A busy_only event from iOS arrives with title/location/notes
    already None client-side — must validate without requiring them."""
    minimal = {
        "external_id": "ek-min",
        "external_modified_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "start_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "end_at": datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
    }
    ev = IOSEventKitEvent.model_validate(minimal)
    assert ev.title is None
    assert ev.location is None
    assert ev.notes is None
    assert ev.attendees_count is None
    assert ev.tombstoned is False


# ---------------------------------------------------------------------------
# LLM exposure floor (PM B-4)


def _project(**over):
    """Convenience caller — defaults to a full-storage row."""
    base = dict(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        all_day=False,
        title="Dr. Patel — annual physical",
        location="Bozeman Health",
        notes="Bring labs",
        attendees_count=2,
        privacy_mode_applied="full_details",
        source_consent=True,
    )
    base.update(over)
    return project_event_for_llm(**base)


def test_llm_floor_consent_false_returns_busy_only_equivalent():
    """Even when title/location/notes are stored on the row, Ask sees
    only start/end/all_day until the user elevates consent. This is
    the second-elevation contract."""
    out = _project(source_consent=False)
    assert set(out.keys()) == {"start_at", "end_at", "all_day"}
    assert "title" not in out
    assert "location" not in out
    assert "notes" not in out


def test_llm_floor_consent_false_even_when_privacy_mode_full():
    """Storage mode does NOT override consent. A row stored under
    full_details but on a source whose consent is false is busy-only
    to the LLM."""
    out = _project(privacy_mode_applied="full_details", source_consent=False)
    assert out == {
        "start_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "end_at": datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        "all_day": False,
    }


def test_llm_consent_true_full_details_storage_exposes_everything():
    out = _project(privacy_mode_applied="full_details", source_consent=True)
    assert out["title"] == "Dr. Patel — annual physical"
    assert out["location"] == "Bozeman Health"
    assert out["notes"] == "Bring labs"
    assert out["attendees_count"] == 2


def test_llm_consent_true_title_and_time_storage_strips_location_and_notes():
    """consent=True doesn't conjure data that wasn't stored. A row
    stored under title_and_time exposes title but NOT location/notes/
    attendees, since those were NULL'd at ingest."""
    out = _project(
        privacy_mode_applied="title_and_time",
        source_consent=True,
        location=None, notes=None, attendees_count=None,
    )
    assert out["title"] == "Dr. Patel — annual physical"
    assert "location" not in out
    assert "notes" not in out
    assert "attendees_count" not in out


def test_llm_consent_true_busy_only_storage_strips_title_too():
    """busy_only stored = nothing more to show even with consent on."""
    out = _project(
        privacy_mode_applied="busy_only",
        source_consent=True,
        title=None, location=None, notes=None, attendees_count=None,
    )
    assert out == {
        "start_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "end_at": datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        "all_day": False,
    }


def test_llm_projector_pure_intersection_of_storage_and_consent():
    """Property-style: for every combination of (storage, consent),
    the projection is the intersection. Spot-check the matrix."""
    times = dict(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        all_day=False,
    )
    fields = dict(
        title="T", location="L", notes="N", attendees_count=1,
    )
    matrix = {
        # (storage, consent) → set of keys expected in projection
        ("busy_only", False):       {"start_at", "end_at", "all_day"},
        ("busy_only", True):        {"start_at", "end_at", "all_day"},
        ("title_and_time", False):  {"start_at", "end_at", "all_day"},
        ("title_and_time", True):   {"start_at", "end_at", "all_day", "title"},
        ("full_details", False):    {"start_at", "end_at", "all_day"},
        ("full_details", True):     {"start_at", "end_at", "all_day", "title",
                                     "location", "notes", "attendees_count"},
    }
    for (storage, consent), expected_keys in matrix.items():
        out = project_event_for_llm(
            **times,
            **fields,
            privacy_mode_applied=storage,  # type: ignore[arg-type]
            source_consent=consent,
        )
        assert set(out.keys()) == expected_keys, (
            f"({storage=}, {consent=}) → expected {expected_keys}, got {set(out.keys())}"
        )


# ---------------------------------------------------------------------------
# Tombstones + 30-day purge (PM B-3)


def test_purge_cutoff_default_is_30_days():
    assert PURGE_TTL_DAYS_DEFAULT == 30


def test_purge_cutoff_subtracts_ttl_from_now():
    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    cutoff = compute_purge_cutoff(ttl_days=30, now=now)
    assert cutoff == now - timedelta(days=30)


def test_purge_cutoff_injectable_for_test_determinism():
    """Production callers default ``now``; tests inject. Ensures the
    test harness can reason about cutoff without time travel."""
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cutoff_short = compute_purge_cutoff(ttl_days=7, now=fixed)
    cutoff_long = compute_purge_cutoff(ttl_days=90, now=fixed)
    assert cutoff_long < cutoff_short < fixed


# ---------------------------------------------------------------------------
# Record-scoping + perimeter (static-source inspection)


def test_create_source_uses_require_role_caregiver():
    """Slice 1 perimeter compliance: writes require caregiver+."""
    from fastapi import Depends
    from ownchart.routes.calendar import create_source

    sig = inspect.signature(create_source)
    ctx_default = sig.parameters["ctx"].default
    assert isinstance(ctx_default, type(Depends(lambda: None)))
    # The dependency callable's repr should reference require_role("caregiver").
    assert "caregiver" in repr(ctx_default.dependency.__qualname__ if hasattr(ctx_default.dependency, '__qualname__') else ctx_default.dependency) \
        or "caregiver" in inspect.getsource(create_source)


@pytest.mark.parametrize(
    "handler_name, required_role",
    [
        ("create_source", "caregiver"),
        ("list_sources", "viewer"),
        ("patch_source", "caregiver"),
        ("disconnect_source", "caregiver"),
        ("ingest_events", "caregiver"),
        ("list_events", "viewer"),
    ],
)
def test_every_route_handler_uses_require_role(handler_name, required_role):
    """Every calendar route's signature must declare an AuthContext
    dependency at the required role — Slice 1 perimeter compliance."""
    from ownchart.routes import calendar as cal_mod
    handler = getattr(cal_mod, handler_name)
    src = inspect.getsource(handler)
    assert f'require_role("{required_role}")' in src, (
        f"{handler_name} does not declare require_role({required_role!r})"
    )


@pytest.mark.parametrize(
    "handler_name",
    ["list_sources", "patch_source", "disconnect_source", "list_events"],
)
def test_record_scoped_reads_filter_by_active_record(handler_name):
    """Reads under /calendar must filter by ``ctx.active_record_id``
    so a caregiver switching to record B doesn't see record A's
    calendars or events."""
    from ownchart.routes import calendar as cal_mod
    src = inspect.getsource(getattr(cal_mod, handler_name))
    assert "ctx.active_record_id" in src, (
        f"{handler_name} doesn't filter by active record"
    )


def test_create_source_stamps_record_id():
    """Writes must stamp ``person_record_id=ctx.active_record_id`` on
    the new row."""
    from ownchart.routes.calendar import create_source
    src = inspect.getsource(create_source)
    assert "person_record_id=ctx.active_record_id" in src


def test_ingest_route_filters_source_by_active_record_id():
    """Ingest must verify the calendar_source belongs to the active
    record before writing events under it — cross-record probe is a
    404, not a 'huh, OK, I'll write to this random source'."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert "CalendarSource.person_record_id == ctx.active_record_id" in src
    # And the event insert must stamp the right record too.
    assert "person_record_id=ctx.active_record_id" in src


def test_ingest_route_runs_redactor_before_insert():
    """The defense-in-depth contract: the server's redactor runs at
    every UPSERT regardless of what iOS sent."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert "redact_event_for_storage(ev, privacy_mode=src.privacy_mode)" in src


def test_ingest_uses_redacted_privacy_mode_applied_on_insert():
    """privacy_mode_applied on the event row must come from the
    redactor's output, not from raw iOS input. Future audits should
    be able to trust the column."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert 'redacted["privacy_mode_applied"]' in src


def test_disconnect_route_cascades_tombstones():
    """DELETE /sources/{id} sets disconnected_at AND
    tombstoned_at=now() on every visible event under the source."""
    from ownchart.routes.calendar import disconnect_source
    src = inspect.getsource(disconnect_source)
    # Single UPDATE on calendar_events that sets tombstoned_at.
    assert "tombstoned_at=now" in src
    assert "CalendarEvent" in src
    # Filtered to events still visible (tombstoned_at IS NULL) so we
    # don't bump the existing tombstone timer.
    assert "tombstoned_at.is_(None)" in src


def test_patch_route_runs_redaction_sweep_on_tightening():
    """A privacy_mode tightening sweep redacts existing events
    immediately, so the user's privacy posture takes effect for
    historical data without requiring a re-sync."""
    from ownchart.routes.calendar import patch_source, _redact_events_for_tightening
    src = inspect.getsource(patch_source)
    # patch_source detects tightening direction and calls the sweep.
    assert "_PRIVACY_RANK" in src
    assert "_redact_events_for_tightening" in src
    # The sweep itself handles busy_only + title_and_time and zeros
    # the right columns.
    sweep_src = inspect.getsource(_redact_events_for_tightening)
    assert 'new_mode == "busy_only"' in sweep_src
    assert 'new_mode == "title_and_time"' in sweep_src
    # Tombstoned rows are skipped — they're already on the purge path.
    assert "tombstoned_at.is_(None)" in sweep_src


def test_list_events_filters_tombstoned():
    """Retrieval window query must hide tombstoned rows."""
    from ownchart.routes.calendar import list_events
    src = inspect.getsource(list_events)
    assert "tombstoned_at.is_(None)" in src
    assert "CalendarEvent.person_record_id == ctx.active_record_id" in src


# ---------------------------------------------------------------------------
# Model wiring (sanity)


def test_calendar_source_model_carries_person_record_id():
    """Slice 1 perimeter parity: ORM column must exist for the
    record-scoped SELECTs to compile at request time."""
    assert hasattr(CalendarSource, "person_record_id")


def test_calendar_event_model_carries_person_record_id():
    assert hasattr(CalendarEvent, "person_record_id")


def test_calendar_event_model_has_tombstoned_at():
    assert hasattr(CalendarEvent, "tombstoned_at")


def test_calendar_source_model_has_llm_full_details_consent():
    """The second-elevation flag must exist on the source row —
    the LLM projector keys off it."""
    assert hasattr(CalendarSource, "llm_full_details_consent")


def test_calendar_event_model_has_privacy_mode_applied():
    """Stored-mode column is what the redaction sweep keys off; must
    exist for the route's `WHERE privacy_mode_applied = ...` clauses
    to compile."""
    assert hasattr(CalendarEvent, "privacy_mode_applied")
