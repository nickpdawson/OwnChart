"""Cross-record leak tests for /api/sources WRITE endpoints.

Beta 1 M02 Slice 1, perimeter rollout Batch 2c.

Pairs with `test_perimeter_sources_read.py` but covers the write
half: the upload/patch/analyze/extract endpoints that mutate state.

What this pins:

  1. Every write handler depends on `require_role("caregiver")`,
     which in turn depends on `get_auth_context`. When the
     AuthContext dependency raises one of the PM-A-5 errors
     (`record_access_revoked`, `no_memberships`), the route
     returns 403 with that code instead of silently 201-ing.

  2. When `get_auth_context` succeeds but the caller's active
     role is `viewer` (below the `caregiver` minimum), the
     `require_role` gate raises 403 `insufficient_role`. This is
     the dimension write tests add over read tests — reads are
     gated to "any member"; writes are gated to "caregiver+".

  3. The handler body NEVER runs on denial. Even an empty/bogus
     multipart body returns 403 before FastAPI parses it, because
     dependency resolution precedes body parsing.

  4. Static signature check: every record-mutating handler must
     declare an `AuthContext` parameter so FastAPI runs the
     dependency chain. Catches accidental drops in refactor.

SQL-stamping property — "newly inserted rows carry
`person_record_id = ctx.active_record_id`" — is verified by code
review of the diff in `routes/sources.py` (look for
`person_record_id=ctx.active_record_id` on every
`SourceDocument(...)`, `EvidenceAnchor(...)`, and `ExtractedFact(...)`
construction inside a `require_role`-gated handler). A live-DB
integration test follows once a real Postgres fixture lands.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


# ---------------------------------------------------------------------------
# Endpoint inventory
#
# Each entry: (label, method, path_factory, kwargs_for_request)
# kwargs is sparse — denied_client must 403 before the body matters,
# so we mostly send empty/minimal bodies. The 415/400 paths inside
# each handler are tested elsewhere (or by the live deploy); this
# file ONLY pins the perimeter contract.

def _id() -> str:
    return str(uuid.uuid4())


WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    # POST /photo — multipart; denied_client trips dep BEFORE form parse
    ("photo", "POST", lambda: "/api/sources/photo",
     {"files": {"file": ("x.jpg", b"\x00", "image/jpeg")}}),
    # POST /note — JSON body
    ("note", "POST", lambda: "/api/sources/note",
     {"json": {"body": "hello"}}),
    # POST /voice — multipart audio
    ("voice", "POST", lambda: "/api/sources/voice",
     {"files": {"file": ("v.m4a", b"\x00", "audio/m4a")}}),
    # POST /pdf — multipart
    ("pdf", "POST", lambda: "/api/sources/pdf",
     {"files": {"file": ("d.pdf", b"\x00", "application/pdf")}}),
    # POST /ccda — multipart
    ("ccda", "POST", lambda: "/api/sources/ccda",
     {"files": {"file": ("c.xml", b"<?xml ?>", "application/xml")}}),
    # POST /auto-export — multipart JSON
    ("auto-export", "POST", lambda: "/api/sources/auto-export",
     {"files": {"file": ("ae.json", b"{}", "application/json")}}),
    # PATCH /{id} — JSON body
    ("patch", "PATCH", lambda: f"/api/sources/{_id()}",
     {"json": {"caption": "x"}}),
    # POST /{id}/analyze — query param only
    ("analyze", "POST", lambda: f"/api/sources/{_id()}/analyze", {}),
    # POST /{id}/extract-facts — JSON body
    ("extract-facts", "POST", lambda: f"/api/sources/{_id()}/extract-facts",
     {"json": {}}),
]


# ---------------------------------------------------------------------------
# 403 on AuthContext denial (record_access_revoked / no_memberships)


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    """User explicitly asked for a record (via header) that their
    membership was revoked on. The dep chain raises BEFORE the role
    gate even runs (because record_access_revoked is raised inside
    `get_auth_context`, which is what `require_role` depends on)."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_write_403_on_no_memberships(
    app_fixture, label, method, path_factory, kwargs,
):
    """User has zero non-revoked memberships. Writes refuse with
    no_memberships — there's no record to stamp inserts against."""
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# 403 insufficient_role — caller is a viewer trying to write
#
# Reads are gated to "any active membership"; writes are gated to
# "caregiver+". A viewer who passes `get_auth_context` must STILL
# be denied at the role gate.


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_write_403_insufficient_role_for_viewer(
    app_fixture, label, method, path_factory, kwargs,
):
    """A viewer membership passes `get_auth_context` (the read
    gate) but `require_role("caregiver")` rejects with
    `insufficient_role`. The handler body never runs."""
    c = authed_client(app_fixture, role="viewer")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role", body
    assert body["detail"]["required"] == "caregiver"
    assert body["detail"]["actual"] == "viewer"


# ---------------------------------------------------------------------------
# Body never runs on denial (structural leak guarantee)
#
# If FastAPI ran the body, it would 500 on the un-migrated test DB
# (the `person_record_id` columns don't exist yet in the test sqlite
# / no DB at all). A clean 403 proves the dep tripped first.


def test_photo_handler_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post(
        "/api/sources/photo",
        files={"file": ("x.jpg", b"fake", "image/jpeg")},
    )
    assert r.status_code == 403


def test_patch_handler_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.patch(
        f"/api/sources/{_id()}",
        json={"caption": "should never be applied"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Static signature check — every write handler must depend on AuthContext.
# If a refactor accidentally drops the parameter, this fires before any
# runtime test can catch the leak.


def test_write_handler_signatures_include_auth_context():
    from typing import get_type_hints

    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.sources import (
        extract_facts_from_source,
        patch_source,
        trigger_photo_analyze,
        upload_auto_export,
        upload_ccda,
        upload_note,
        upload_pdf,
        upload_photo,
        upload_voice,
    )

    # Exhaustive list — adding a new sources write without wiring
    # AuthContext will fail this test once added here.
    handlers = (
        upload_photo,
        upload_note,
        upload_voice,
        upload_pdf,
        upload_ccda,
        upload_auto_export,
        patch_source,
        trigger_photo_analyze,
        extract_facts_from_source,
    )
    for fn in handlers:
        hints = get_type_hints(fn)
        ctx_params = [
            name for name, hint in hints.items()
            if hint is AuthContext
        ]
        assert len(ctx_params) == 1, (
            f"{fn.__name__} must declare exactly one "
            f"`AuthContext` parameter; got {ctx_params}."
        )


# ---------------------------------------------------------------------------
# Cross-record GET on a write target also denies
#
# A caregiver writing to record A then immediately trying to read
# their write via record B (via header switch) must 404. The read
# tests already cover GET /api/sources/{id} but pinning the
# write→read symmetry here makes the contract auditable in one file.


def test_cross_record_get_after_write_denies(app_fixture):
    """Write under one record context, read under another → 404
    (or 403 if the second context is denied entirely). Either way
    the cross-record read never returns the row.

    We can't actually exercise the write half without a real DB,
    so this proves the read half of the symmetry: a denied auth
    context cannot retrieve any source id, even one freshly
    inserted in another scope."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get(f"/api/sources/{_id()}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


# ---------------------------------------------------------------------------
# FU-EXTRACT-PERIMETER-MISS regression — extract-facts row must stamp
# person_record_id.
#
# Migration 0031 made extraction_jobs.person_record_id NOT NULL. The
# ORM model and the extract_facts_from_source route both missed the
# stamp during Batch 2c rollout, so every kickoff 500'd with a
# NotNullViolationError. Two-line fix, pinned here so the constructor
# call doesn't regress.


def test_extraction_job_model_declares_person_record_id():
    """ExtractionJob's mapped columns must include person_record_id
    so SQLAlchemy emits it on INSERT (no column → SQLAlchemy silently
    omits → 500 against the NOT NULL constraint)."""
    from ownchart.models.extraction_job import ExtractionJob
    cols = {c.name for c in ExtractionJob.__table__.columns}
    assert "person_record_id" in cols, (
        "ExtractionJob model missing person_record_id; SQLAlchemy "
        "will omit it from INSERT and hit the NOT NULL constraint."
    )
    col = ExtractionJob.__table__.c.person_record_id
    assert col.nullable is False, (
        "person_record_id must be NOT NULL on the model to match the "
        "DB constraint set by migration 0031."
    )


def test_extract_facts_route_stamps_person_record_id():
    """extract_facts_from_source must pass
    person_record_id=ctx.active_record_id to the ExtractionJob
    constructor. Static-source check — without it, the INSERT
    omits the column and the route 500s."""
    import inspect
    from ownchart.routes.sources import extract_facts_from_source
    src = inspect.getsource(extract_facts_from_source)
    # The constructor call must include the perimeter stamp.
    assert "person_record_id=ctx.active_record_id" in src, (
        "extract_facts_from_source must stamp "
        "person_record_id=ctx.active_record_id on the ExtractionJob "
        "it creates; otherwise the INSERT hits a NOT NULL violation."
    )
