"""M02 Slice 4 — export skeleton tests.

Pure-function + static-source coverage. No DB, no LLM, no TestClient
deep-integration. The DB-side checks for the runner / route pipelines
are static-source inspections (same pattern as Slice 3's
test_calendar_slice3.py).

Coverage of the four contracts PM specified:
  1. Owner / caregiver only on writes; viewer + on reads.
  2. Record-scoped (cross-record 404; person_record_id stamped).
  3. 72-hour TTL (computed at completion; purge helper hard-deletes).
  4. Five audit event types fire across the lifecycle.

Plus contract pins on:
  - canonical JSON deterministic output.
  - human-readable TXT structure (sections + "(none)" fallbacks).
  - snapshot builder filters every collection by person_record_id
    AND excludes tombstoned calendar events.
  - no API-side hard delete (only the purge worker hard-deletes).
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from ownchart.exports import (
    EXPORT_AUDIT_EVENT_TYPES,
    EXPORT_COMPLETED,
    EXPORT_DELETED,
    EXPORT_DOWNLOADED,
    EXPORT_FAILED,
    EXPORT_REQUESTED,
    EXPORT_TTL_HOURS,
    ExportSnapshot,
    canonical_ownchart_json_mapper,
    compute_export_expiry,
    human_readable_txt_mapper,
)
from ownchart.exports.audit import EXPORT_SUBJECT_TYPE
from ownchart.exports.expiry import purge_expired_exports
from ownchart.exports.snapshot import build_export_snapshot
from ownchart.models import ExportFile, ExportJob, FILE_TYPES, JOB_STATUSES, REQUESTED_FORMATS


# ---------------------------------------------------------------------------
# Helpers


def _empty_snapshot(generated_at: datetime | None = None) -> ExportSnapshot:
    return ExportSnapshot(
        generated_at=generated_at or datetime(2026, 5, 19, 3, 0, tzinfo=timezone.utc),
        record={
            "id": "rec-1",
            "display_name": "Me",
            "is_self": True,
        },
    )


# ---------------------------------------------------------------------------
# Audit event types — five constants, exactly


def test_export_audit_has_six_event_types_after_slice4_hardening():
    """Slice 4 hardening (PM 2026-05-19): the audit constants are
    SIX, not five. EXPORT_EXPIRED was added so the TTL purge worker
    can emit an audit BEFORE hard-deleting the row. EXPORT_FAILED
    stays — real failures deserve audit too."""
    from ownchart.exports import EXPORT_EXPIRED
    assert EXPORT_AUDIT_EVENT_TYPES == (
        "export_requested",
        "export_completed",
        "export_failed",
        "export_downloaded",
        "export_deleted",
        "export_expired",
    )
    # And the individual constants are referenced by routes / runner
    # / purge without any value drift.
    assert EXPORT_REQUESTED == "export_requested"
    assert EXPORT_COMPLETED == "export_completed"
    assert EXPORT_FAILED == "export_failed"
    assert EXPORT_DOWNLOADED == "export_downloaded"
    assert EXPORT_DELETED == "export_deleted"
    assert EXPORT_EXPIRED == "export_expired"


def test_export_subject_type_is_pinned():
    """One subject_type string for all five events — the activity
    UI keys off this. A typo would split the timeline."""
    assert EXPORT_SUBJECT_TYPE == "export_job"


def test_routes_use_the_five_user_initiated_audit_event_types():
    """Static check: the routes module references the FIVE
    user-initiated audit event-type constants. EXPORT_EXPIRED is
    intentionally NOT here — it's system-initiated and emitted by
    the TTL purge worker (see test_purge_emits_export_expired_*
    below)."""
    from ownchart.routes import exports as exports_routes
    src = inspect.getsource(exports_routes)
    user_initiated = (
        "EXPORT_REQUESTED",
        "EXPORT_COMPLETED",
        "EXPORT_FAILED",
        "EXPORT_DOWNLOADED",
        "EXPORT_DELETED",
    )
    for const_name in user_initiated:
        assert const_name in src, (
            f"routes module never references {const_name}"
        )
    # And the system-attributed EXPORT_EXPIRED stays OUT of the
    # routes module — purges run from the worker, not from a user
    # request.
    assert "EXPORT_EXPIRED" not in src, (
        "EXPORT_EXPIRED appears in routes module — it should only be "
        "emitted by the purge worker (purge_expired_exports)"
    )


def test_purge_module_references_export_expired_constant():
    """The system-initiated audit event is emitted from the purge
    worker; verify the constant is referenced there."""
    from ownchart.exports.expiry import purge_expired_exports
    src = inspect.getsource(purge_expired_exports)
    assert "EXPORT_EXPIRED" in src


# ---------------------------------------------------------------------------
# Expiry — 72 hours, injectable


def test_export_ttl_is_72_hours_per_pm_c6():
    assert EXPORT_TTL_HOURS == 72


def test_compute_export_expiry_adds_72h_to_completed_at():
    completed = datetime(2026, 5, 19, 3, 0, tzinfo=timezone.utc)
    expires = compute_export_expiry(completed_at=completed)
    assert expires - completed == timedelta(hours=72)


def test_compute_export_expiry_default_now():
    """When called without completed_at the helper bases off now().
    The exact value isn't predictable; just verify it's in the
    future and within tolerance of the 72h window."""
    before = datetime.now(timezone.utc)
    expires = compute_export_expiry()
    after = datetime.now(timezone.utc)
    # expires should be (now-ish) + 72h.
    assert expires >= before + timedelta(hours=72)
    assert expires <= after + timedelta(hours=72)


def test_purge_function_is_async_and_takes_db_session():
    """The 72h purge runs as an arq job (when wired). Signature
    must be async and accept a db session (positional) + ttl
    injection points for tests."""
    sig = inspect.signature(purge_expired_exports)
    assert inspect.iscoroutinefunction(purge_expired_exports)
    assert "db" in sig.parameters
    assert "now" in sig.parameters


# ---------------------------------------------------------------------------
# Model wiring (sanity)


def test_model_constants_match_migration_enums():
    """ORM constants are the single source of truth other modules
    import; their values must match the CHECK constraints in the
    migration."""
    assert JOB_STATUSES == ("pending", "running", "completed", "failed")
    assert REQUESTED_FORMATS == ("ownchart_json", "txt", "all")
    assert FILE_TYPES == ("ownchart_json", "txt")


def test_export_job_has_record_scope_column():
    """Slice 1 perimeter parity — every record-bearing table carries
    person_record_id at the ORM layer so SELECTs can filter on it
    at request time."""
    assert hasattr(ExportJob, "person_record_id")


def test_export_file_has_record_scope_column():
    assert hasattr(ExportFile, "person_record_id")


def test_export_job_has_lifecycle_columns():
    for col in (
        "status", "requested_at", "started_at", "completed_at",
        "failed_at", "expires_at", "deleted_at", "error_message",
    ):
        assert hasattr(ExportJob, col), f"ExportJob missing {col}"


def test_export_file_has_content_addressed_columns():
    for col in ("byte_size", "sha256", "storage_uri", "file_type"):
        assert hasattr(ExportFile, col), f"ExportFile missing {col}"


# ---------------------------------------------------------------------------
# Canonical JSON mapper — byte-deterministic


def test_canonical_json_is_valid_utf8_json():
    payload = canonical_ownchart_json_mapper(_empty_snapshot())
    parsed = json.loads(payload.decode("utf-8"))
    assert parsed["snapshot_version"] == "1.0"
    assert parsed["record"]["display_name"] == "Me"


def test_canonical_json_uses_sort_keys_for_diffability():
    payload = canonical_ownchart_json_mapper(_empty_snapshot()).decode("utf-8")
    # Keys at the top level must appear in sorted order. A spot
    # check that 'calendar_events' precedes 'calendar_sources' which
    # precedes 'facts' which precedes 'generated_at' ... etc.
    expected_keys_in_order = [
        "calendar_events",
        "calendar_sources",
        "facts",
        "generated_at",
        "record",
        "snapshot_version",
        "sources",
    ]
    last_pos = 0
    for key in expected_keys_in_order:
        pos = payload.find(f'"{key}"')
        assert pos > last_pos, (
            f"key {key!r} appears at pos {pos}, expected after {last_pos}"
        )
        last_pos = pos


def test_canonical_json_is_byte_deterministic():
    """Same input → byte-identical output. Required for diffing
    snapshots, content-addressed storage, sha256 stability."""
    a = canonical_ownchart_json_mapper(_empty_snapshot())
    b = canonical_ownchart_json_mapper(_empty_snapshot())
    assert a == b


def test_canonical_json_emits_iso_8601_datetimes():
    """The default ``json.dumps`` cannot serialize datetime; the
    mapper's custom serializer must produce ISO-8601. Pydantic v2's
    json mode normalizes UTC offsets to the ``Z`` form, which is
    valid ISO-8601 and what diffing tools / re-importers should
    expect — pin it so a future refactor that swaps to ``+00:00``
    fails this and forces an explicit migration."""
    payload = canonical_ownchart_json_mapper(_empty_snapshot()).decode("utf-8")
    assert '"generated_at": "2026-05-19T03:00:00Z"' in payload


# ---------------------------------------------------------------------------
# Human-readable TXT mapper


def test_txt_contains_all_section_headers():
    payload = human_readable_txt_mapper(_empty_snapshot()).decode("utf-8")
    for section in [
        "OwnChart Export",
        "Record — Me",
        "Sources (0)",
        "Facts (0)",
        "Calendar sources (0)",
        "Calendar events (0)",
    ]:
        assert section in payload, f"section missing: {section}"


def test_txt_empty_sections_render_explicit_none():
    """Absence is visible — an empty section says "(none)" rather
    than leaving the reader to guess whether something failed."""
    payload = human_readable_txt_mapper(_empty_snapshot()).decode("utf-8")
    # Four data sections (sources, facts, calendar sources, calendar events).
    assert payload.count("(none)") == 4


def test_txt_is_valid_utf8():
    """Sanity: human-readable bytes are decodable as UTF-8."""
    payload = human_readable_txt_mapper(_empty_snapshot())
    decoded = payload.decode("utf-8")
    assert isinstance(decoded, str)


def test_txt_includes_record_full_name_when_present():
    snap = ExportSnapshot(
        generated_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
        record={
            "id": "r1", "display_name": "Mom",
            "given_names": "Diane", "family_name": "Walker",
            "is_self": False,
        },
    )
    out = human_readable_txt_mapper(snap).decode("utf-8")
    assert "Diane Walker" in out


def test_txt_handles_facts_grouped_by_year():
    snap = ExportSnapshot(
        generated_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
        record={"id": "r1", "display_name": "Me", "is_self": True},
        facts=[
            {
                "id": "f1", "fact_type": "observation",
                "label": "Test fact A",
                "date_start": datetime(2025, 6, 1, tzinfo=timezone.utc),
                "review_state": "confirmed",
                "created_at": datetime(2025, 6, 2, tzinfo=timezone.utc),
            },
            {
                "id": "f2", "fact_type": "observation",
                "label": "Test fact B",
                "date_start": datetime(2026, 2, 1, tzinfo=timezone.utc),
                "review_state": "needs_review",
                "created_at": datetime(2026, 2, 2, tzinfo=timezone.utc),
            },
            {
                "id": "f3", "fact_type": "observation",
                "label": "Undated fact",
                "review_state": "confirmed",
                "created_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
            },
        ],
    )
    out = human_readable_txt_mapper(snap).decode("utf-8")
    assert "-- 2025 --" in out
    assert "-- 2026 --" in out
    assert "-- undated --" in out
    assert "Test fact A" in out
    assert "Test fact B" in out


# ---------------------------------------------------------------------------
# Snapshot builder — structural / scoping contract (static source)


def test_snapshot_builder_filters_every_collection_by_record():
    """Static source check: build_export_snapshot's SELECTs MUST
    include WHERE person_record_id == person_record_id for every
    record-bearing collection. A regression that drops this filter
    would leak cross-record data into an export."""
    src = inspect.getsource(build_export_snapshot)
    # One filter per record-bearing collection (sources, facts,
    # calendar_sources, calendar_events).
    assert src.count(".person_record_id == person_record_id") >= 4


def test_snapshot_builder_excludes_tombstoned_calendar_events():
    """Tombstoned events are on the 30d purge path — exports should
    reflect the user's current view, not the soft-delete shadow."""
    src = inspect.getsource(build_export_snapshot)
    assert "CalendarEvent.tombstoned_at.is_(None)" in src


def test_snapshot_model_collections_default_to_empty_list():
    """A record with no data still serializes — collections are
    Optional[list[T]] = []. Avoids None-versus-empty edge cases in
    the mappers."""
    snap = ExportSnapshot(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        record={"id": "r1", "display_name": "Me", "is_self": True},
    )
    assert snap.sources == []
    assert snap.facts == []
    assert snap.calendar_sources == []
    assert snap.calendar_events == []


# ---------------------------------------------------------------------------
# Routes — perimeter denial (no DB needed)


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST",   "/api/exports", {"requested_format": "all"}),
        ("GET",    "/api/exports", None),
        ("GET",    "/api/exports/00000000-0000-0000-0000-000000000000", None),
        ("GET",    "/api/exports/00000000-0000-0000-0000-000000000000/download"
                   "?file_type=ownchart_json", None),
        ("DELETE", "/api/exports/00000000-0000-0000-0000-000000000000", None),
    ],
)
def test_export_routes_403_on_record_access_revoked(app_fixture, method, path, body):
    """Every export route propagates 403 record_access_revoked."""
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
        ("POST", "/api/exports", {"requested_format": "all"}),
        ("GET",  "/api/exports", None),
    ],
)
def test_export_routes_403_on_no_memberships(app_fixture, method, path, body):
    """User with zero memberships → 403 no_memberships (not 401, not
    a silent 200) on the routes that don't take an export_id path
    param (those return 404 by design for cross-record probes)."""
    from ownchart.tests.conftest import denied_client
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path, json=body)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Routes — require_role at correct level (static)


@pytest.mark.parametrize(
    "handler_name, required_role",
    [
        ("create_export", "caregiver"),
        ("list_exports", "caregiver"),
        ("get_export", "caregiver"),
        ("download_export", "caregiver"),
        ("delete_export", "caregiver"),
    ],
)
def test_every_export_handler_uses_require_role(handler_name, required_role):
    """Slice 4 hardening (PM 2026-05-19, review #2): ALL endpoints
    are caregiver+. Reads were viewer+ before review; PM's "owner/
    caregiver only" framing applies to reads AND writes. The export
    is a privileged operation regardless of direction."""
    from ownchart.routes import exports as exports_routes
    handler = getattr(exports_routes, handler_name)
    src = inspect.getsource(handler)
    assert f'require_role("{required_role}")' in src, (
        f"{handler_name} doesn't declare require_role({required_role!r})"
    )


def test_no_export_handler_uses_viewer_role():
    """Regression guard: a future refactor that re-opens reads to
    viewers would loosen the privacy posture. The static check
    above tests the positive side; this one tests the negative."""
    from ownchart.routes import exports as exports_routes
    src = inspect.getsource(exports_routes)
    assert 'require_role("viewer")' not in src, (
        "exports route module references viewer role — Slice 4 hardening "
        "requires caregiver+ on every endpoint"
    )


# ---------------------------------------------------------------------------
# Routes — record scoping (static)


@pytest.mark.parametrize(
    "handler_name",
    ["list_exports", "get_export", "download_export", "delete_export"],
)
def test_record_scoped_reads_filter_by_active_record(handler_name):
    """Every record-scoped read on /api/exports/* filters by
    ctx.active_record_id."""
    from ownchart.routes import exports as exports_routes
    src = inspect.getsource(getattr(exports_routes, handler_name))
    assert "ctx.active_record_id" in src, (
        f"{handler_name} doesn't filter by ctx.active_record_id"
    )


def test_create_export_stamps_record_id_on_new_job():
    """Writes must stamp ``person_record_id=ctx.active_record_id``
    on the new ExportJob row."""
    from ownchart.routes.exports import create_export
    src = inspect.getsource(create_export)
    assert "person_record_id=ctx.active_record_id" in src
    assert "user_id=ctx.user.id" in src


# ---------------------------------------------------------------------------
# Routes — 72h TTL set at completion (via runner)


def test_runner_sets_expires_at_via_compute_export_expiry():
    """The runner uses compute_export_expiry — not a hard-coded
    timedelta — so the TTL constant in expiry.py is the single
    source of truth."""
    from ownchart.exports.runner import run_export_job
    src = inspect.getsource(run_export_job)
    assert "compute_export_expiry" in src
    assert "job.expires_at" in src


def test_runner_re_raises_on_failure_so_route_can_audit_failed():
    """On runner exception, the route emits EXPORT_FAILED. The
    runner must re-raise after transitioning the job to ``failed``."""
    from ownchart.exports.runner import run_export_job
    src = inspect.getsource(run_export_job)
    assert "raise" in src
    assert 'job.status = "failed"' in src
    assert 'job.status = "completed"' in src


def test_runner_is_idempotent_on_already_terminal_job():
    """Re-running a completed/failed job is a no-op — protects
    against an arq retry of a job whose first run succeeded."""
    from ownchart.exports.runner import run_export_job
    src = inspect.getsource(run_export_job)
    assert 'job.status in ("completed", "failed")' in src


# ---------------------------------------------------------------------------
# No API-side hard delete (only purge worker hard-deletes)


def test_routes_module_does_not_contain_orm_delete_on_export_jobs():
    """Slice 4 contract: DELETE /api/exports/{id} is soft-delete only
    (deleted_at). The 72h purge worker is the only path that hard-
    deletes the row. Routes module must not contain delete(ExportJob)
    SQL."""
    from ownchart.routes import exports as exports_routes
    src = inspect.getsource(exports_routes)
    assert "delete(ExportJob)" not in src
    assert "DELETE FROM export_jobs" not in src


def test_delete_route_soft_deletes_via_update():
    """DELETE /api/exports/{id} sets deleted_at via UPDATE; the row
    stays until the 72h purge."""
    from ownchart.routes.exports import delete_export
    src = inspect.getsource(delete_export)
    assert "update(ExportJob)" in src
    assert "deleted_at=now" in src
    # And on-disk files are removed up-front so the user-visible
    # delete is honored immediately.
    assert "delete_job_files_on_disk" in src


def test_purge_function_hard_deletes_via_orm_delete():
    """The purge worker is the ONLY hard-delete path; it uses
    delete(ExportJob) on rows past TTL."""
    src = inspect.getsource(purge_expired_exports)
    assert "delete(ExportJob)" in src
    # And the predicate filters by status == 'completed' AND
    # expires_at past — not all jobs, never pending/running.
    assert 'ExportJob.status == "completed"' in src
    assert "ExportJob.expires_at" in src


# ---------------------------------------------------------------------------
# Routes — download path (audit + 404 hygiene)


def test_download_route_emits_audit_event_and_returns_file_response():
    from ownchart.routes.exports import download_export
    src = inspect.getsource(download_export)
    assert "EXPORT_DOWNLOADED" in src
    assert "FileResponse" in src
    # 404 on cross-record / missing job; 409 on not-yet-completed;
    # 410 on file-on-disk-missing. All three statuses appear.
    assert "HTTP_404_NOT_FOUND" in src
    assert "HTTP_409_CONFLICT" in src
    assert "HTTP_410_GONE" in src


def test_download_route_takes_file_type_query_param():
    """The download URL specifies which file (json or txt) via a
    query param, not a path segment — keeps the URL stable as the
    file_type set grows in future (Pictal in M03)."""
    from ownchart.routes.exports import download_export
    sig = inspect.signature(download_export)
    assert "file_type" in sig.parameters


# ---------------------------------------------------------------------------
# OpenAPI shape


def test_openapi_create_export_request_advertises_requested_format():
    from ownchart.routes.exports import CreateExportRequest
    schema = CreateExportRequest.model_json_schema()
    props = schema["properties"]
    assert "requested_format" in props
    # Default landed.
    assert props["requested_format"].get("default") == "all"


def test_openapi_export_job_out_surfaces_lifecycle_fields():
    """A client reading the spec must see status + the timestamps
    + the expiry + the files list — all of the user-facing
    lifecycle surface."""
    from ownchart.routes.exports import ExportJobOut
    schema = ExportJobOut.model_json_schema()
    props = schema["properties"]
    for field in (
        "status", "requested_at", "completed_at", "failed_at",
        "expires_at", "error_message", "files",
    ):
        assert field in props, f"ExportJobOut missing field: {field}"


# ---------------------------------------------------------------------------
# Slice 4 hardening — review-finding regression tests (PM 2026-05-19)


def test_download_rejects_expired_export_with_410():
    """Finding #3: download_export must check expires_at < now()
    and return 410 BEFORE serving bytes. The user-visible contract
    is '72 hours then gone' regardless of whether the hourly purge
    worker has run yet."""
    from ownchart.routes.exports import download_export
    src = inspect.getsource(download_export)
    assert "job.expires_at" in src
    assert "HTTP_410_GONE" in src
    # Belt and suspenders — verify the comparison uses `<` not `>`,
    # so a refactor that flips the operator fails this.
    assert "job.expires_at < datetime.now" in src


def test_purge_emits_export_expired_audit_per_row():
    """Finding #5: the purge worker emits EXPORT_EXPIRED for every
    expiring row BEFORE the hard delete, so the audit trail records
    when the file became unreachable, not just when the user asked
    for deletion."""
    from ownchart.exports.expiry import purge_expired_exports
    src = inspect.getsource(purge_expired_exports)
    # AuditEvent insert with the right event_type
    assert "EXPORT_EXPIRED" in src
    assert "AuditEvent(" in src
    # And user_id is NULL — system-attributed
    assert "user_id=None" in src
    # Audit insert happens BEFORE the row delete.
    audit_pos = src.index("EXPORT_EXPIRED")
    delete_pos = src.index("delete(ExportJob)")
    assert audit_pos < delete_pos, (
        "audit emission must precede delete — otherwise a row-delete "
        "failure would leave the system silent about the attempt"
    )


def test_purge_removes_on_disk_files_before_row_delete():
    """Finding #4: purge must call delete_job_files_on_disk before
    deleting the DB row, otherwise rows go away but bytes stay
    behind under <data_dir>/exports/<job_id>/."""
    from ownchart.exports.expiry import purge_expired_exports
    src = inspect.getsource(purge_expired_exports)
    assert "delete_job_files_on_disk" in src
    # FS call happens BEFORE the audit insert (which is itself before
    # the row delete).
    fs_pos = src.index("delete_job_files_on_disk")
    audit_pos = src.index("EXPORT_EXPIRED")
    assert fs_pos < audit_pos, (
        "FS cleanup must precede audit insert so that 'audit + delete' "
        "stays atomic in the DB transaction; a later FS failure can't "
        "block the audit trail"
    )


def test_purge_accepts_data_dir_parameter():
    """The purge needs a data_dir to know where the export files
    live. None disables the FS sweep (for tests / dry-run)."""
    sig = inspect.signature(purge_expired_exports)
    assert "data_dir" in sig.parameters
    assert sig.parameters["data_dir"].default is None


def test_purge_is_per_row_loop_not_single_delete():
    """The pre-hardening shape used a single delete().where() that
    couldn't observe which rows died; the hardened shape SELECTs
    expiring rows first, then loops. Static check: the source
    contains a `select(ExportJob)` before the `delete(ExportJob)`."""
    from ownchart.exports.expiry import purge_expired_exports
    src = inspect.getsource(purge_expired_exports)
    sel_pos = src.index("select(ExportJob)")
    del_pos = src.index("delete(ExportJob)")
    assert sel_pos < del_pos


def test_txt_contains_patient_disclaimer_section():
    """Finding #7: TXT mapper renders a 'Patient packet — please
    read' section with the disclaimer text right after the header,
    before any data sections.

    The disclaimer text is line-wrapped at ~72 cols for printability,
    so multi-word clauses may straddle line breaks. Whitespace-
    flatten the payload before substring matching to make the test
    robust to wrapping choices.
    """
    from ownchart.exports.mappers import EXPORT_DISCLAIMER
    payload = human_readable_txt_mapper(_empty_snapshot()).decode("utf-8")
    assert "Patient packet — please read" in payload
    flattened = " ".join(payload.split())
    # The four load-bearing "NOT" clauses must all appear (post-wrap).
    assert "NOT a medical record" in flattened
    assert "NOT a legal document" in flattened
    assert "NOT a clinical care recommendation" in flattened
    assert "NOT covered by HIPAA" in flattened
    # And the disclaimer constant itself shows up.
    fragment = EXPORT_DISCLAIMER.split(". ")[0]  # first sentence
    assert fragment in flattened


def test_txt_disclaimer_appears_before_record_section():
    """The disclaimer is positioned before the Record section so
    anyone glancing at the file sees framing first."""
    payload = human_readable_txt_mapper(_empty_snapshot()).decode("utf-8")
    disclaimer_pos = payload.index("Patient packet — please read")
    record_pos = payload.index("Record — Me")
    assert disclaimer_pos < record_pos


def test_json_contains_top_level_disclaimer_key():
    """Finding #7: JSON mapper surfaces the same disclaimer text
    under a top-level 'disclaimer' key. A future re-import / tool
    can preserve the framing."""
    from ownchart.exports.mappers import EXPORT_DISCLAIMER
    payload = canonical_ownchart_json_mapper(_empty_snapshot())
    data = json.loads(payload.decode("utf-8"))
    assert "disclaimer" in data
    assert data["disclaimer"] == EXPORT_DISCLAIMER


def test_disclaimer_text_contains_four_load_bearing_not_clauses():
    """The disclaimer constant itself carries the four 'NOT'
    clauses verbatim. A softening refactor that drops one would
    fail this test."""
    from ownchart.exports.mappers import EXPORT_DISCLAIMER
    for clause in (
        "NOT a medical record",
        "NOT a legal document",
        "NOT a clinical care recommendation",
        "NOT covered by HIPAA",
    ):
        assert clause in EXPORT_DISCLAIMER, (
            f"EXPORT_DISCLAIMER no longer contains: {clause!r}"
        )


# ---------------------------------------------------------------------------
# Finding #6 — no-secrets regression test on both mapper outputs


# Strings that MUST NOT appear in exported JSON or TXT bytes,
# under any rendering. Lowercase for case-insensitive matching.
_BANNED_SUBSTRINGS = (
    "password",
    "password_hash",
    "token",          # catches access_token / refresh_token /
                      # session_token / auto_export_token / device_token
    "access_token",
    "refresh_token",
    "session_id",
    "session_token",
    "secret",
    "session_secret",
    "credential",
    "credentials",
    "anthropic_api_key",
    "api_key",
    "bearer",
    "client_secret",
    "private_key",
)


def _rich_snapshot_with_user_data() -> ExportSnapshot:
    """A non-trivial snapshot that exercises every collection.
    Useful for verifying mapper outputs against the banned-strings
    list, since an empty snapshot couldn't possibly contain
    credential surface."""
    now = datetime(2026, 5, 19, 3, 0, tzinfo=timezone.utc)
    return ExportSnapshot(
        generated_at=now,
        record={
            "id": "rec-1",
            "display_name": "Me",
            "given_names": "Nick",
            "family_name": "Dawson",
            "is_self": True,
        },
        sources=[{
            "id": "src-1",
            "source_type": "native_healthkit",
            "source_label": "Apple Watch — 2026-01-15",
            "source_system": "HealthKit",
            "original_filename": "native-healthkit-2026-01-15.batch",
            "acquired_at": now,
            "created_at": now,
        }],
        facts=[{
            "id": "f-1",
            "fact_type": "observation",
            "label": "Running — 36 min, 8.0 km",
            "description": "Workout",
            "date_start": now,
            "date_end": now,
            "review_state": "confirmed",
            "coded_concepts": {
                "healthkit_identifier": "HKWorkoutType",
                "workout_activity_type": "running",
                "source_bundle_id": "com.apple.health.DE49D92E",
            },
            "confidence": 95,
            "significance": "major_activity_lifestyle",
            "significance_source": "heuristic",
            "created_at": now,
        }],
        calendar_sources=[{
            "id": "cs-1",
            "adapter_type": "ios_eventkit",
            "display_name": "Apps (Nick)",
            "privacy_mode": "title_and_time",
            "llm_full_details_consent": False,
            "connected_at": now,
        }],
        calendar_events=[{
            "id": "ce-1",
            "calendar_source_id": "cs-1",
            "title": "Dentist appointment",
            "start_at": now,
            "end_at": now,
            "all_day": False,
            "privacy_mode_applied": "title_and_time",
        }],
    )


@pytest.mark.parametrize("banned", _BANNED_SUBSTRINGS)
def test_json_export_does_not_leak_banned_substring(banned: str):
    """Finding #6 — regression guard: the canonical JSON output
    must never contain any string that suggests credentials /
    sessions / tokens / secrets are leaking through the snapshot
    builder.

    A future field addition to ExportSnapshot that accidentally
    surfaces (say) ``user.password_hash`` or
    ``auto_export_tokens.token_hash`` will fail this test."""
    payload = canonical_ownchart_json_mapper(
        _rich_snapshot_with_user_data()
    )
    text = payload.decode("utf-8").lower()
    assert banned not in text, (
        f"JSON export contains banned substring {banned!r} — possible "
        f"credential / token / session leak"
    )


@pytest.mark.parametrize("banned", _BANNED_SUBSTRINGS)
def test_txt_export_does_not_leak_banned_substring(banned: str):
    """Same regression guard on the human-readable TXT output."""
    payload = human_readable_txt_mapper(_rich_snapshot_with_user_data())
    text = payload.decode("utf-8").lower()
    assert banned not in text, (
        f"TXT export contains banned substring {banned!r} — possible "
        f"credential / token / session leak"
    )


def test_snapshot_model_does_not_import_credential_models():
    """Defense in depth: the snapshot module imports only the data
    models a user can see. A future refactor that drags in User /
    DeviceToken / AutoExportToken / OAuthSession etc. would risk
    accidental serialization of secrets. Verify by reading the
    module source — banned imports never appear."""
    import ownchart.exports.snapshot as snap_mod
    src = inspect.getsource(snap_mod)
    for banned_model in (
        "from ..models.device_token",
        "from ..models.auto_export_token",
        "from ..models.llm_provider_credential",
        "from ..models.oauth_session",
        "from ..models.provider_connection",
    ):
        assert banned_model not in src, (
            f"snapshot module imports {banned_model} — credential models "
            "must stay out of the export shape"
        )
