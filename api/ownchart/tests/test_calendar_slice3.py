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
    dedupe_events_for_projection,
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
    every key.

    Stored under the namespaced ``raw_metadata.calendar.sample_metadata``
    sub-key (mirrors Slice 2's ``raw_metadata.healthkit.*`` pattern).
    """
    md = {"recurrence": "weekly_tuesday", "ek_color": "#0066CC"}
    for mode in PRIVACY_MODES_TUPLE:
        out = redact_event_for_storage(
            _event_dict(metadata=md), privacy_mode=mode,
        )
        assert out["raw_metadata"]["calendar"]["sample_metadata"] == md


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
    every UPSERT regardless of what iOS sent. sync_mode flows from
    the request body into the redactor so the per-row audit trail
    records which mode landed each row."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert "redact_event_for_storage(" in src
    assert "privacy_mode=src.privacy_mode" in src
    assert "sync_mode=body.sync_mode" in src


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


# ---------------------------------------------------------------------------
# Closeout — sync_mode contract (PM Slice 3 closeout point 1)


def test_ingest_request_accepts_sync_mode():
    """`/api/calendar/ingest` accepts sync_mode in the request body
    and defaults to "incremental" for old iOS builds that don't send
    it. Mirrors the Slice 2 HK pattern."""
    from ownchart.routes.calendar import CalendarIngestRequest
    body = CalendarIngestRequest(
        calendar_source_id="00000000-0000-0000-0000-000000000000",
        events=[],
    )
    assert body.sync_mode == "incremental"
    body_bf = CalendarIngestRequest(
        calendar_source_id="00000000-0000-0000-0000-000000000000",
        events=[], sync_mode="backfill",
    )
    assert body_bf.sync_mode == "backfill"


def test_sync_mode_lands_in_raw_metadata_calendar():
    """sync_mode flows through to per-row audit trail so a future
    investigation can tell whether a given event landed during
    backfill or incremental sync."""
    for mode in ("backfill", "incremental"):
        out = redact_event_for_storage(
            _event_dict(), privacy_mode="full_details", sync_mode=mode,
        )
        assert out["raw_metadata"]["calendar"]["sync_mode_at_ingest"] == mode


def test_sync_mode_does_not_affect_user_visible_fields():
    """BE-3 mode-agnostic invariant carried over: same input under
    backfill and incremental must produce identical storage modulo
    sync_mode_at_ingest."""
    bf = redact_event_for_storage(
        _event_dict(), privacy_mode="full_details", sync_mode="backfill",
    )
    inc = redact_event_for_storage(
        _event_dict(), privacy_mode="full_details", sync_mode="incremental",
    )
    bf_meta = bf.pop("raw_metadata")
    inc_meta = inc.pop("raw_metadata")
    assert bf == inc
    assert bf_meta["calendar"].pop("sync_mode_at_ingest") == "backfill"
    assert inc_meta["calendar"].pop("sync_mode_at_ingest") == "incremental"
    assert bf_meta == inc_meta


def test_ingest_route_logs_window_for_audit():
    """PM closeout point 3: iOS is the deletion authority, but the
    scanned window is logged for audit. Verify the route source
    threads ``body.window_start_at`` and ``body.window_end_at``
    through to the batch log."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert "window_start_at" in src
    assert "window_end_at" in src


# ---------------------------------------------------------------------------
# Closeout — backend-controlled LLM exposure (PM Slice 3 closeout point 2)


def test_llm_projector_signature_does_not_accept_ios_supplied_consent():
    """PM closeout point 2: full-details Ask exposure is backend-
    controlled. ``project_event_for_llm``'s consent argument is
    ``source_consent`` — the caller is expected to pass the value
    read from ``calendar_sources.llm_full_details_consent`` on the
    SERVER row, NOT an iOS-supplied request flag. This is a structural
    test: the projector must not be invokable with ios-supplied
    keyword (i.e., we don't accidentally rename the arg in a way
    that suggests it's iOS-controlled)."""
    sig = inspect.signature(project_event_for_llm)
    assert "source_consent" in sig.parameters
    # No "ios" / "client" / "device" keyword present — that would
    # suggest the projector trusts user-side data.
    for kw in ("ios_consent", "client_consent", "device_consent",
               "user_supplied_consent"):
        assert kw not in sig.parameters


def test_llm_projector_consent_must_be_explicit_boolean():
    """Consent is a boolean; no truthy-coercion shortcuts. A future
    refactor that accidentally types this as ``Any`` and relies on
    truthiness could let "false-y but truthy" values (e.g., the
    string "true" coming back as truthy) silently elevate. Pin the
    annotation.

    ``eval_str=True`` resolves PEP 563 lazy string annotations
    (which the project uses via ``from __future__ import annotations``)
    back to the real type object.
    """
    sig = inspect.signature(project_event_for_llm, eval_str=True)
    assert sig.parameters["source_consent"].annotation is bool


# ---------------------------------------------------------------------------
# Closeout — iOS as deletion authority (PM Slice 3 closeout point 3)


def test_redact_passes_tombstoned_through_for_route_dispatch_only():
    """The redactor passes ``tombstoned`` so the route can branch
    to the soft-delete UPDATE. The redactor itself doesn't set
    tombstoned_at — that's the route's responsibility (and only
    when iOS explicitly sets the flag)."""
    out = redact_event_for_storage(
        _event_dict(tombstoned=True), privacy_mode="full_details",
    )
    assert out["tombstoned"] is True
    # The redactor doesn't write tombstoned_at — that column is set
    # by the route via UPDATE, not by the UPSERT VALUES().
    assert "tombstoned_at" not in out


def test_ingest_route_only_tombstones_on_explicit_flag():
    """Absence from a batch is NOT a delete signal. The route only
    sets tombstoned_at when iOS explicitly sets ``tombstoned: true``."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    # The tombstone branch is gated on the redacted dict's flag,
    # which came directly from the iOS sample. No "rows not seen
    # in this window" logic — that's PM's anti-pattern.
    assert 'redacted["tombstoned"]' in src
    # And the route does NOT loop over existing events to mark
    # missing ones tombstoned. (Negative assertion.)
    assert "missing" not in src.lower() or "missing" not in src.lower().split("tombstone")[0]


def test_purge_function_is_the_only_hard_delete_path():
    """Slice 3 contract: hard-delete happens via the 30d purge
    function on tombstoned rows, never via the API. The route file
    must not contain a `DELETE FROM calendar_events` or
    `delete(CalendarEvent)` outside of the purge module."""
    from ownchart.routes import calendar as cal_mod
    src = inspect.getsource(cal_mod)
    # No SQL DELETE on calendar_events from the route layer.
    assert "delete(CalendarEvent" not in src
    assert "DELETE FROM calendar_events" not in src
    # The route's import of `delete` is used for SQL UPDATE chaining
    # only; the actual hard-delete lives in calendar_eventkit.py.


# ---------------------------------------------------------------------------
# Multi-calendar support per (user, record, adapter)


def test_calendar_sources_unique_on_external_id_not_just_adapter():
    """Slice 3 supports MULTIPLE active calendar sources per (user,
    record, adapter). The UNIQUE constraint distinguishes by
    external_id so a user can hold a personal calendar + work
    calendar + Family calendar + shared-trip calendar simultaneously.
    """
    from sqlalchemy import inspect as sqla_inspect
    cs_table = CalendarSource.__table__
    uniques = [c for c in cs_table.constraints
               if c.__class__.__name__ == "UniqueConstraint"]
    assert len(uniques) >= 1
    # The unique key spans (user, record, adapter, external_id) so
    # multiple external_ids under the same (user, record, adapter)
    # ARE allowed.
    found = False
    for u in uniques:
        cols = sorted(c.name for c in u.columns)
        if cols == sorted([
            "user_id", "person_record_id", "adapter_type", "external_id",
        ]):
            found = True
            break
    assert found, (
        f"calendar_sources unique constraint must span (user, record, "
        f"adapter, external_id); got: "
        f"{[sorted(c.name for c in u.columns) for u in uniques]}"
    )


def test_multi_calendar_payloads_keep_distinct_external_ids():
    """A user binding four calendars (e.g. personal, work, family,
    shared-trip) under one record produces four distinct payloads,
    one per iOS calendar identifier. Verify the wire shape doesn't
    inadvertently collapse them — the redactor passes external_id
    through verbatim and four redact calls produce four distinct
    `external_id` fields."""
    redacted = []
    for cal_id in ("ek-personal", "ek-work", "ek-family", "ek-shared-trip"):
        ev = _event_dict(external_id=f"{cal_id}-evt-1")
        redacted.append(
            redact_event_for_storage(ev, privacy_mode="full_details")
        )
    assert len({r["external_id"] for r in redacted}) == 4


def test_at_least_four_distinct_sources_can_carry_events_for_one_record():
    """A more stringent multi-calendar check: simulate the redact
    output for four calendars under one record, with overlapping
    events (e.g. holidays on both Personal and Family). Verify
    storage keeps every per-source row distinct (no implicit
    collapse at the redactor level) and that the projection-time
    deduper can collapse the cross-calendar duplicates."""
    fixed_start = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    fixed_end = datetime(2026, 7, 4, 13, tzinfo=timezone.utc)
    rows: list[dict] = []
    for cal_id, cal_label in [
        ("ek-personal", "personal"),
        ("ek-work", "work"),
        ("ek-family", "family"),
        ("ek-shared-trip", "shared-trip"),
    ]:
        rows.append(redact_event_for_storage(
            _event_dict(
                external_id=f"{cal_id}-holiday-2026-07-04",
                start_at=fixed_start, end_at=fixed_end,
                title="Independence Day",
                ical_uid="holiday-2026-07-04",  # same UID across all four
                time_zone="America/Denver",
            ),
            privacy_mode="full_details",
        ))
    # Storage keeps four rows — each per-source row is distinct.
    assert len(rows) == 4
    assert len({r["external_id"] for r in rows}) == 4
    # Projection-time dedup collapses them to one (same ical_uid).
    deduped = dedupe_events_for_projection(rows)
    assert len(deduped) == 1


# ---------------------------------------------------------------------------
# Dedupe posture (PM Slice 3 closeout note)


def test_dedupe_uses_ical_uid_when_present():
    """Canonical key: RFC 5545 UID. Two rows with the same UID but
    different external_id (e.g. same invite on personal + work
    calendars) collapse to one in the projection."""
    a = redact_event_for_storage(
        _event_dict(external_id="cal-A-evt", ical_uid="abc-uid"),
        privacy_mode="full_details",
    )
    b = redact_event_for_storage(
        _event_dict(external_id="cal-B-evt", ical_uid="abc-uid"),
        privacy_mode="full_details",
    )
    assert len(dedupe_events_for_projection([a, b])) == 1


def test_dedupe_falls_back_to_fingerprint_when_uid_absent():
    """No UID → conservative fingerprint: (normalized title, start,
    end, time_zone). Same title at the same time in the same TZ
    collapses; different times don't."""
    base_kw = dict(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        time_zone="America/Denver",
    )
    a = redact_event_for_storage(
        _event_dict(external_id="cal-A", ical_uid=None, title="Dentist", **base_kw),
        privacy_mode="full_details",
    )
    b = redact_event_for_storage(
        _event_dict(external_id="cal-B", ical_uid=None, title="DENTIST  ", **base_kw),
        privacy_mode="full_details",
    )
    # Normalized title comparison collapses casing + trailing space.
    assert len(dedupe_events_for_projection([a, b])) == 1


def test_dedupe_does_not_collapse_different_times():
    a = redact_event_for_storage(
        _event_dict(
            external_id="cal-A", ical_uid=None, title="Dentist",
            start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        ),
        privacy_mode="full_details",
    )
    b = redact_event_for_storage(
        _event_dict(
            external_id="cal-B", ical_uid=None, title="Dentist",
            start_at=datetime(2026, 1, 16, 10, tzinfo=timezone.utc),
            end_at=datetime(2026, 1, 16, 11, tzinfo=timezone.utc),
        ),
        privacy_mode="full_details",
    )
    assert len(dedupe_events_for_projection([a, b])) == 2


def test_dedupe_distinguishes_time_zones():
    """An Eastern-tz standup at 10am and a Pacific-tz standup at the
    same UTC instant shouldn't collapse — they're different events
    even if the wall-clock happens to look similar."""
    common = dict(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        ical_uid=None,  # force fingerprint path
        title="standup",
    )
    a = redact_event_for_storage(
        _event_dict(external_id="cal-A", time_zone="America/New_York", **common),
        privacy_mode="full_details",
    )
    b = redact_event_for_storage(
        _event_dict(external_id="cal-B", time_zone="America/Los_Angeles", **common),
        privacy_mode="full_details",
    )
    assert len(dedupe_events_for_projection([a, b])) == 2


def test_dedupe_first_seen_wins():
    """When two rows collapse, the first in iteration order is the
    representative. Callers responsible for ordering the input by
    whatever priority matters (most-recent ``external_modified_at``,
    primary calendar first, etc.) before dedup."""
    a = redact_event_for_storage(
        _event_dict(external_id="cal-A-first", ical_uid="uid-xyz"),
        privacy_mode="full_details",
    )
    b = redact_event_for_storage(
        _event_dict(external_id="cal-B-second", ical_uid="uid-xyz"),
        privacy_mode="full_details",
    )
    out = dedupe_events_for_projection([a, b])
    assert len(out) == 1
    assert out[0]["external_id"] == "cal-A-first"


def test_dedupe_is_projection_time_only_no_storage_writes():
    """Static check: the dedup helper is a pure list → list function,
    not async, doesn't take a db parameter. Slice 3 contract: storage
    keeps every per-source row; dedup happens at projection."""
    sig = inspect.signature(dedupe_events_for_projection)
    assert not inspect.iscoroutinefunction(dedupe_events_for_projection)
    assert "db" not in sig.parameters
    assert "session" not in sig.parameters


def test_iosevent_accepts_ical_uid_and_time_zone():
    """Wire shape closeout: iOS can now send ical_uid + time_zone."""
    ev = IOSEventKitEvent.model_validate(_event_dict(
        ical_uid="ical-uid-001", time_zone="America/Denver",
    ))
    assert ev.ical_uid == "ical-uid-001"
    assert ev.time_zone == "America/Denver"


def test_iosevent_ical_uid_and_time_zone_default_to_none():
    """Pre-closeout iOS builds that don't send these fields stay
    backward-compatible — defaults are None, projector falls back
    to the fingerprint path."""
    ev = IOSEventKitEvent.model_validate({
        "external_id": "ek-1",
        "external_modified_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "start_at": datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        "end_at": datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
    })
    assert ev.ical_uid is None
    assert ev.time_zone is None


# ---------------------------------------------------------------------------
# Secondary acceptance — perimeter denial coverage (no iOS needed)
#
# Every calendar route propagates Slice 1 perimeter HTTPExceptions
# (403 record_access_revoked / 403 no_memberships / 401 from missing
# session). conftest.py's `denied_client` raises the 403 at
# `get_auth_context`, so every record-scoped handler must return 403
# rather than silently 200ing.


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/api/calendar/sources",
         {"adapter_type": "ios_eventkit",
          "external_id": "ek-cal-X", "display_name": "X"}),
        ("GET",  "/api/calendar/sources", None),
        ("PATCH", "/api/calendar/sources/00000000-0000-0000-0000-000000000000",
         {"privacy_mode": "busy_only"}),
        ("DELETE", "/api/calendar/sources/00000000-0000-0000-0000-000000000000",
         None),
        ("POST", "/api/calendar/ingest",
         {"calendar_source_id": "00000000-0000-0000-0000-000000000000",
          "events": []}),
        ("GET",  "/api/calendar/events"
                "?start_at=2026-01-01T00:00:00Z"
                "&end_at=2026-12-31T23:59:59Z",
         None),
    ],
)
def test_calendar_routes_403_on_record_access_revoked(
    app_fixture, method, path, body,
):
    """Every record-scoped calendar route returns 403 when AuthContext
    raises ``record_access_revoked`` — the Slice 1 perimeter contract."""
    from ownchart.tests.conftest import denied_client
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path, json=body)
    assert r.status_code == 403, (
        f"{method} {path} should propagate 403 record_access_revoked; "
        f"got {r.status_code}"
    )


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/api/calendar/sources",
         {"adapter_type": "ios_eventkit",
          "external_id": "ek-cal-X", "display_name": "X"}),
        ("GET",  "/api/calendar/sources", None),
        ("POST", "/api/calendar/ingest",
         {"calendar_source_id": "00000000-0000-0000-0000-000000000000",
          "events": []}),
        ("GET",  "/api/calendar/events"
                "?start_at=2026-01-01T00:00:00Z"
                "&end_at=2026-12-31T23:59:59Z",
         None),
    ],
)
def test_calendar_routes_403_on_no_memberships(app_fixture, method, path, body):
    """User with zero non-revoked memberships → 403 no_memberships
    (NOT 401, NOT a silent 200) on every calendar route."""
    from ownchart.tests.conftest import denied_client
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path, json=body)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Secondary acceptance — cascade tombstone path
# (DB-level integration deferred to when a real DB fixture lands;
#  static-source inspection pins the contract.)


def test_disconnect_sets_disconnected_at_on_source_row():
    """Soft-disconnect must set disconnected_at on the source row
    itself (not just tombstone the events)."""
    from ownchart.routes.calendar import disconnect_source
    src = inspect.getsource(disconnect_source)
    assert "src.disconnected_at = now" in src


def test_disconnect_cascade_skips_already_tombstoned_events():
    """The cascade UPDATE filters by tombstoned_at IS NULL so it
    doesn't bump the timestamp on events that were tombstoned earlier
    (e.g. by an iOS-side delete). Important: the 30d purge worker
    keys off tombstoned_at age, and we shouldn't reset that clock
    just because the source was disconnected later."""
    from ownchart.routes.calendar import disconnect_source
    src = inspect.getsource(disconnect_source)
    # The UPDATE statement filters by IS NULL before setting tombstoned_at.
    assert "CalendarEvent.calendar_source_id == src.id" in src
    assert "CalendarEvent.tombstoned_at.is_(None)" in src
    assert "values(tombstoned_at=now" in src


def test_disconnect_returns_204_no_content():
    """REST contract — soft-disconnect is a status change, returns 204.
    The handler returns None; FastAPI's status_code=204 decorator
    turns that into an empty body."""
    from ownchart.routes.calendar import disconnect_source
    sig = inspect.signature(disconnect_source, eval_str=True)
    assert sig.return_annotation is None
    # And the route decorator pins 204.
    src = inspect.getsource(disconnect_source)
    assert "HTTP_204_NO_CONTENT" in src or "status_code=204" in src


# ---------------------------------------------------------------------------
# Secondary acceptance — resurrection (re-bind a disconnected source)


def test_create_source_resurrects_on_conflict():
    """Re-creating a source by the same (user, record, adapter,
    external_id) tuple must REUSE the existing row (idempotent
    upsert) AND clear disconnected_at so the source goes back to
    active. The user-visible promise: 'I unpicked this calendar
    last week, now I want it back' — the UUID stays stable, the
    historical events are still there, the source just goes back
    on."""
    from ownchart.routes.calendar import create_source
    src = inspect.getsource(create_source)
    # The UPSERT uses the unique constraint as the conflict target.
    assert "calendar_sources_user_record_adapter_external_uq" in src
    # And on conflict, disconnected_at is reset to None.
    assert '"disconnected_at": None' in src
    # connected_at is bumped to now so the source's "active since"
    # timestamp reflects the resurrection.
    assert '"connected_at": now' in src


def test_create_source_does_not_use_orm_get_or_create():
    """Static guard against a refactor that uses SELECT-then-INSERT
    (race-vulnerable) instead of the atomic pg_insert.on_conflict_do_update
    that handles concurrent re-binds correctly."""
    from ownchart.routes.calendar import create_source
    src = inspect.getsource(create_source)
    assert "pg_insert" in src
    assert "on_conflict_do_update" in src


def test_create_source_stamps_user_id_and_record_id_at_creation():
    """A resurrection must NOT change the original user_id /
    person_record_id — those are part of the UNIQUE key so the
    UPSERT only matches a row that already had the same pair; but
    the VALUES still set them explicitly so a fresh INSERT path
    also stamps correctly."""
    from ownchart.routes.calendar import create_source
    src = inspect.getsource(create_source)
    assert "user_id=ctx.user.id" in src
    assert "person_record_id=ctx.active_record_id" in src


# ---------------------------------------------------------------------------
# Secondary acceptance — privacy tightening sweep
# (Full SQL-execution test deferred to DB-fixture; static source
#  inspection pins the SQL shape.)


def test_tightening_to_busy_only_zeros_all_four_visible_columns():
    """The route's redaction sweep on privacy_mode → busy_only must
    NULL out all four user-visible columns in one UPDATE."""
    from ownchart.routes.calendar import _redact_events_for_tightening
    src = inspect.getsource(_redact_events_for_tightening)
    # Find the busy_only branch and confirm it sets all four to None.
    busy_branch = src.split('new_mode == "busy_only"')[1].split("elif")[0]
    for col in ("title=None", "location=None", "notes=None",
                "attendees_count=None"):
        assert col in busy_branch, f"busy_only branch missing {col}"
    assert 'privacy_mode_applied="busy_only"' in busy_branch


def test_tightening_to_title_and_time_strips_only_location_notes_attendees():
    """The title_and_time branch strips location/notes/attendees from
    full_details rows but leaves title alone. AND it only touches
    rows where privacy_mode_applied == 'full_details' — busy_only
    rows are already stricter and must NOT be loosened."""
    from ownchart.routes.calendar import _redact_events_for_tightening
    src = inspect.getsource(_redact_events_for_tightening)
    tat_branch = src.split('new_mode == "title_and_time"')[1]
    # Stripped columns:
    for col in ("location=None", "notes=None", "attendees_count=None"):
        assert col in tat_branch, f"title_and_time branch missing {col}"
    # Title is preserved (NOT set to None in this branch).
    assert "title=None" not in tat_branch.split("# full_details")[0]
    # And the WHERE clause guards against accidentally loosening
    # busy_only rows.
    assert "privacy_mode_applied == \"full_details\"" in tat_branch


def test_tightening_sweep_skips_tombstoned_events():
    """Tombstoned events are on the path to the 30d purge; we don't
    re-touch them with a privacy sweep. Both privacy branches must
    filter tombstoned_at IS NULL."""
    from ownchart.routes.calendar import _redact_events_for_tightening
    src = inspect.getsource(_redact_events_for_tightening)
    # Count the IS_NULL filter occurrences — one per privacy branch.
    assert src.count("tombstoned_at.is_(None)") >= 2


def test_patch_only_invokes_sweep_when_rank_decreases():
    """A LOOSENING (e.g. busy_only → title_and_time, or
    title_and_time → full_details) must NOT call the sweep —
    loosening doesn't conjure data that wasn't stored, and we
    don't want to touch rows for no reason."""
    from ownchart.routes.calendar import patch_source
    src = inspect.getsource(patch_source)
    # The comparison is "rank[new] < rank[old]" — strictness rank
    # increases with looseness, so a tightening drops the rank.
    assert "_PRIVACY_RANK[body.privacy_mode] < _PRIVACY_RANK[src.privacy_mode]" in src
    # And the sweep is only called when the comparison sets the
    # tightening flag, not on every PATCH.
    assert "if tightening_to is not None:" in src


def test_privacy_rank_pins_strictness_order():
    """Confirm the rank: busy_only is strictest (rank 0),
    title_and_time middle (1), full_details loosest (2). A future
    refactor that swaps these would silently invert the tightening
    detection."""
    from ownchart.routes.calendar import _PRIVACY_RANK
    assert _PRIVACY_RANK == {"busy_only": 0,
                             "title_and_time": 1,
                             "full_details": 2}


# ---------------------------------------------------------------------------
# Secondary acceptance — elevated LLM exposure (consent flip)


def test_llm_projector_consent_flip_changes_visibility_in_place():
    """Same row, same stored fields, different consent → different
    projection. This is the property the consent UI promises: the
    user can toggle the LLM-visibility bit without re-syncing /
    re-storing anything."""
    common = dict(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        all_day=False,
        title="Dr. Patel",
        location="Bozeman Health",
        notes="Bring labs",
        attendees_count=2,
        privacy_mode_applied="full_details",
    )
    out_off = project_event_for_llm(**common, source_consent=False)
    out_on = project_event_for_llm(**common, source_consent=True)
    # Toggling consent expanded the keyset by exactly the stored
    # fields the privacy_mode allowed.
    added = set(out_on.keys()) - set(out_off.keys())
    assert added == {"title", "location", "notes", "attendees_count"}


@pytest.mark.parametrize("storage_mode", PRIVACY_MODES_TUPLE)
def test_llm_consent_off_is_invariant_to_storage_mode(storage_mode):
    """Regardless of how the data was stored, consent=False projects
    the busy-only-equivalent set. This is the floor."""
    out = project_event_for_llm(
        start_at=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        end_at=datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
        all_day=False,
        title="T", location="L", notes="N", attendees_count=1,
        privacy_mode_applied=storage_mode,
        source_consent=False,
    )
    assert set(out.keys()) == {"start_at", "end_at", "all_day"}


# ---------------------------------------------------------------------------
# OpenAPI shape — verify the new fields appear in the schema


def test_openapi_calendar_ingest_request_advertises_sync_mode():
    """A client reading the OpenAPI spec must see that
    /api/calendar/ingest accepts sync_mode (with allowed values)."""
    from ownchart.routes.calendar import CalendarIngestRequest
    schema = CalendarIngestRequest.model_json_schema()
    props = schema["properties"]
    assert "sync_mode" in props
    # And the new audit-window fields.
    assert "window_start_at" in props
    assert "window_end_at" in props


def test_openapi_calendar_source_create_advertises_consent_flag():
    """The CalendarSourceCreateRequest must surface
    llm_full_details_consent so the UI can render the two-elevation
    toggle. Default false."""
    from ownchart.routes.calendar import CalendarSourceCreateRequest
    schema = CalendarSourceCreateRequest.model_json_schema()
    props = schema["properties"]
    assert "llm_full_details_consent" in props
    assert props["llm_full_details_consent"].get("default") is False


def test_openapi_calendar_source_out_returns_consent_flag():
    """Reads must echo the consent flag back so the UI knows its
    current state."""
    from ownchart.routes.calendar import CalendarSourceOut
    schema = CalendarSourceOut.model_json_schema()
    assert "llm_full_details_consent" in schema["properties"]


# ---------------------------------------------------------------------------
# FU-CAL-SOURCE-STATUS — sync health + event counts on CalendarSourceOut


def test_calendar_source_out_advertises_sync_status_fields():
    """The four fields the web settings UI needs are present on
    every read of a CalendarSource: last_sync_at, last_sync_status,
    visible_event_count, stored_event_count."""
    from ownchart.routes.calendar import CalendarSourceOut
    props = CalendarSourceOut.model_json_schema()["properties"]
    for field in (
        "last_sync_at",
        "last_sync_status",
        "visible_event_count",
        "stored_event_count",
    ):
        assert field in props, f"CalendarSourceOut missing {field}"


def test_calendar_source_out_count_defaults_are_zero():
    """A bare CalendarSourceOut without explicit counts must default
    to 0/0 — counts are computed at read time, never NULL."""
    from ownchart.routes.calendar import CalendarSourceOut
    s = CalendarSourceOut(
        id="00000000-0000-0000-0000-000000000000",
        adapter_type="ios_eventkit",
        external_id="ek-1",
        display_name="Test",
        privacy_mode="title_and_time",
        llm_full_details_consent=False,
        connected_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        disconnected_at=None,
    )
    assert s.visible_event_count == 0
    assert s.stored_event_count == 0
    assert s.last_sync_at is None
    assert s.last_sync_status is None


def test_ingest_route_stamps_last_sync_status_ok_on_nonempty_batch():
    """A batch with at least one accepted or tombstoned event must
    set last_sync_status='ok' on the source. Static-source check."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    assert "src.last_sync_at = now" in src
    assert '"ok"' in src and '"empty"' in src
    # The branch direction — "ok" when something moved, "empty" when nothing did.
    assert "(accepted + tombstoned_count) > 0" in src


def test_ingest_route_stamps_last_sync_status_empty_on_zero_event_batch():
    """A zero-event batch (iOS scanned the window and saw nothing)
    is still a successful sync — record it as 'empty' so the UI can
    show 'last sync: just now (calendar is empty)' rather than
    'never synced'. Same static-source check as the ok branch."""
    from ownchart.routes.calendar import ingest_events
    src = inspect.getsource(ingest_events)
    # The expression maps (0,0) → 'empty' and (≥1, *) or (*, ≥1) → 'ok'.
    # The single conditional in the source covers both.
    assert (
        '"ok" if (accepted + tombstoned_count) > 0 else "empty"'
        in " ".join(src.split())
    )


def test_list_sources_computes_counts_in_one_query():
    """list_sources must not N+1 — counts come from a single grouped
    query keyed on calendar_source_id. Static-source check that the
    helper is called once with all source IDs."""
    from ownchart.routes.calendar import list_sources
    src = inspect.getsource(list_sources)
    assert "_event_counts_by_source(db, [r.id for r in rows])" in src


def test_event_counts_helper_uses_filter_for_visible():
    """The visible count must filter out tombstoned rows; the stored
    count must not. Both come from one grouped query."""
    from ownchart.routes.calendar import _event_counts_by_source
    src = inspect.getsource(_event_counts_by_source)
    # Visible uses FILTER (tombstoned_at IS NULL); stored does not.
    assert ".filter(CalendarEvent.tombstoned_at.is_(None))" in src
    assert "group_by(CalendarEvent.calendar_source_id)" in src


def test_disconnect_does_not_clear_last_sync_at():
    """Disconnect is a soft state change; the historical last_sync_at
    stays so the user (or operator) can still see when the source
    was actively syncing. Don't reset history just because the user
    paused the source."""
    from ownchart.routes.calendar import disconnect_source
    src = inspect.getsource(disconnect_source)
    assert "last_sync_at" not in src
    assert "last_sync_status" not in src
