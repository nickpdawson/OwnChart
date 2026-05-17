"""Cross-record leak tests for /api/sources read endpoints.

Beta 1 M02 Slice 1, perimeter rollout Batch 2.

What this pins:

  1. `GET /api/sources` and `GET /api/sources/{id}` depend on
     `get_auth_context`, so when the AuthContext dependency raises
     one of the PM-A-5 errors (`record_access_revoked`,
     `no_memberships`, `insufficient_role`), the route returns 403
     with that code instead of silently 200-ing.

  2. The route NEVER executes its handler body when AuthContext
     denies. This is the structural cross-record leak guarantee:
     even if the SQL filter were buggy, the dependency would deny
     before the body runs.

Pattern reused for every subsequent perimeter-rollout batch:
each batch gets a `test_perimeter_<file>_<read|write>.py` that
hits each endpoint with `denied_client` and asserts 403 + the
right error code.

Note: this does NOT exercise the SQL WHERE clause that filters
by `person_record_id`. That requires a live DB fixture; deferred
per PM directive. Code-review the diff in `routes/sources.py` for
the `.where(SourceDocument.person_record_id == ctx.active_record_id)`
literal.
"""

from __future__ import annotations

import uuid

import pytest

from ownchart.tests.conftest import denied_client


# ---------------------------------------------------------------------------
# /api/sources (list)


def test_list_sources_403_on_record_access_revoked(app_fixture):
    """User explicitly asked for a record (via header) that their
    membership was revoked on. AuthContext raises 403; route
    propagates the response intact."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/sources")
    assert r.status_code == 403, r.text
    body = r.json()
    # Body shape per the catch-all HTTPException handler:
    # {"detail": {"code": ..., "message": ...}}
    assert body["detail"]["code"] == "record_access_revoked"


def test_list_sources_403_on_no_memberships(app_fixture):
    """User has zero non-revoked memberships. /api/sources cannot
    return data because there's no record to scope to. 403
    no_memberships; client routes to recovery UI."""
    c = denied_client(app_fixture, code="no_memberships")
    r = c.get("/api/sources")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# /api/sources/{id} (detail)


def test_get_source_403_on_record_access_revoked(app_fixture):
    """Detail endpoint propagates the same AuthContext failure."""
    c = denied_client(app_fixture, code="record_access_revoked")
    fake_id = str(uuid.uuid4())
    r = c.get(f"/api/sources/{fake_id}")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_get_source_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    fake_id = str(uuid.uuid4())
    r = c.get(f"/api/sources/{fake_id}")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


def test_get_source_propagates_unauthenticated(app_fixture):
    """When `get_user_from_device_token_or_session` raises 401
    (upstream of AuthContext), the route 401s — existing iOS
    contract preserved."""
    from fastapi import HTTPException, status

    from ownchart.core.device_auth import (
        get_user_from_device_token_or_session,
    )

    async def _raise_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    app_fixture.dependency_overrides[
        get_user_from_device_token_or_session
    ] = _raise_401

    from fastapi.testclient import TestClient
    c = TestClient(app_fixture)
    r = c.get("/api/sources")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Body never runs on denial (structural leak guarantee)


def test_list_sources_handler_does_not_run_on_denial(app_fixture):
    """If the AuthContext dependency raises, FastAPI returns the
    error BEFORE executing the handler. Even a SQL-filter bug in
    the body couldn't leak data, because the body never runs.

    Verified by: deny → 403 returned without any side-effect.
    No mocks of the route's internal DB session — if the body
    had run, the test would 500 (Postgres column person_record_id
    doesn't exist on the un-migrated test DB)."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/sources")
    # 403 from the dep, NOT 500 from a body that touched
    # un-migrated columns.
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Dependency identity check — make sure the route is actually wired
# to `get_auth_context` and not somehow bypassing it.


def test_list_sources_handler_signature_includes_auth_context():
    """Static check: the route handler must declare a parameter
    typed as `AuthContext` so FastAPI runs the dependency. If a
    refactor accidentally drops the parameter, this test fires
    BEFORE the runtime tests catch the leak.

    `sources.py` uses `from __future__ import annotations` so
    annotations are stored as strings at function-definition
    time. We resolve them via `typing.get_type_hints` for an
    identity check."""
    from typing import get_type_hints

    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.sources import (
        get_extraction_status,
        get_page_image,
        get_source,
        get_source_contribution_summary,
        get_source_review_summary,
        get_thumbnail,
        list_anchors,
        list_sources,
    )

    # Every record-scoped sources GET handler must depend on
    # AuthContext. The list is exhaustive — adding a new sources GET
    # without wiring AuthContext will fail this test once added here.
    handlers = (
        list_sources,
        get_source,
        get_extraction_status,
        list_anchors,
        get_page_image,
        get_thumbnail,
        get_source_contribution_summary,
        get_source_review_summary,
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
# Derived GETs propagate denial


@pytest.mark.parametrize("path_factory,code", [
    (lambda sid: f"/api/sources/{sid}/extraction-status", "record_access_revoked"),
    (lambda sid: f"/api/sources/{sid}/anchors", "record_access_revoked"),
    (lambda sid: f"/api/sources/{sid}/contribution-summary", "no_memberships"),
    (lambda sid: f"/api/sources/{sid}/review-summary", "record_access_revoked"),
])
def test_derived_source_gets_403_on_denial(app_fixture, path_factory, code):
    """Every derived sources GET (status, anchors, contribution-summary,
    review-summary) must propagate the 403 from AuthContext. Page +
    thumb return FileResponse so they're tested separately."""
    c = denied_client(app_fixture, code=code)
    sid = str(uuid.uuid4())
    r = c.get(path_factory(sid))
    assert r.status_code == 403, (
        f"{path_factory(sid)} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == code


def test_page_image_403_on_denial(app_fixture):
    """File-response endpoints (page, thumb) also propagate the
    AuthContext error as a 403 JSON. FastAPI handles the
    HTTPException -> JSONResponse conversion via our catch-all."""
    c = denied_client(app_fixture, code="record_access_revoked")
    sid = str(uuid.uuid4())
    r = c.get(f"/api/sources/{sid}/page/1")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_thumb_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    sid = str(uuid.uuid4())
    r = c.get(f"/api/sources/{sid}/thumb/md")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"
