"""Shared test fixtures (Beta 1 M02 perimeter rollout).

The pre-M02 tests are pure-function (no DB, no HTTP). The
perimeter rollout adds a thin TestClient + dependency-override
layer so each route-batch can prove its perimeter contract
without spinning up a real Postgres in the test runner.

Two layers of fixtures:

  1. `client` — bare FastAPI TestClient with no overrides. Useful
     for `/healthz` and other unauthenticated endpoints.
  2. `client_for_user(user_id, memberships, role_on_active, active_record_id)`
     — TestClient where `get_auth_context` is overridden to return
     a synthetic AuthContext, and `get_user_from_device_token_or_session`
     is overridden to return a synthetic User. The route's internal
     DB queries STILL hit the real session; tests that need to
     exercise those paths use the live demo DB. Tests that don't
     need DB access (perimeter contract checks) use the `denied_*`
     fixtures that raise the M02-specified HTTPExceptions before
     the route body runs.

This is "perimeter-contract" testing — it pins that every
record-scoped route propagates AuthContext failures (403
no_memberships / 403 record_access_revoked / 403 insufficient_role)
as 403 responses. Deep SQL-filter testing (the actual cross-record
data exclusion) follows once a real DB fixture lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient


def _import_app():
    # Lazy import so test collection doesn't fail when the
    # api container isn't yet provisioned.
    from ownchart.main import app
    return app


def _synthetic_user(
    *,
    user_id: uuid.UUID | None = None,
    email: str = "test@example.local",
    is_instance_admin: bool = False,
    default_person_record_id: uuid.UUID | None = None,
) -> Any:
    from ownchart.models.user import User
    return User(
        id=user_id or uuid.uuid4(),
        email=email,
        password_hash="not-used-in-tests",
        phi_consent_granted=True,
        is_instance_admin=is_instance_admin,
        display_name=None,
        default_person_record_id=default_person_record_id,
    )


def _synthetic_record(
    *,
    record_id: uuid.UUID | None = None,
    display_name: str = "Test Record",
    created_by_user_id: uuid.UUID | None = None,
) -> Any:
    from ownchart.models.person_record import PersonRecord
    return PersonRecord(
        id=record_id or uuid.uuid4(),
        display_name=display_name,
        is_self=True,
        created_by_user_id=created_by_user_id or uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def app_fixture():
    """Yield the FastAPI app, then clear any dependency overrides
    the test set on it."""
    app = _import_app()
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(app_fixture) -> TestClient:
    """Bare TestClient with no auth overrides."""
    return TestClient(app_fixture)


# ---------------------------------------------------------------------------
# Auth-override factories


def authed_client(
    app,
    *,
    user_id: uuid.UUID | None = None,
    active_record_id: uuid.UUID | None = None,
    role: str = "owner",
    memberships: list[Any] | None = None,
    is_instance_admin: bool = False,
) -> TestClient:
    """Return a TestClient where auth + auth_context dependencies
    are overridden to a synthetic happy-path user with one record.

    Pass `memberships=...` to simulate multiple records.
    """
    from ownchart.core.auth_context import AuthContext, get_auth_context
    from ownchart.core.device_auth import get_user_from_device_token_or_session

    uid = user_id or uuid.uuid4()
    rid = active_record_id or uuid.uuid4()

    user = _synthetic_user(
        user_id=uid,
        is_instance_admin=is_instance_admin,
        default_person_record_id=rid,
    )
    record = _synthetic_record(record_id=rid, created_by_user_id=uid)
    ctx = AuthContext(
        user=user,
        active_person_record=record,
        active_role=role,  # type: ignore[arg-type]
    )

    async def _user_dep():
        return user

    async def _ctx_dep():
        return ctx

    app.dependency_overrides[get_user_from_device_token_or_session] = _user_dep
    app.dependency_overrides[get_auth_context] = _ctx_dep
    return TestClient(app)


def denied_client(
    app,
    *,
    code: str = "record_access_revoked",
    http_status: int = status.HTTP_403_FORBIDDEN,
) -> TestClient:
    """Return a TestClient where `get_auth_context` raises the
    M02-specified HTTPException. Used to verify each record-scoped
    route propagates the 403 rather than silently 200ing.

    `code` is the JSON `detail.code` field per PM A-5:
      - 'record_access_revoked' — user lost access to the active record.
      - 'no_memberships'        — user has zero memberships.
      - 'insufficient_role'     — caller's role is below required.
    """
    from ownchart.core.auth_context import get_auth_context
    from ownchart.core.device_auth import get_user_from_device_token_or_session

    user = _synthetic_user()

    async def _user_dep():
        return user

    async def _ctx_dep():
        raise HTTPException(
            status_code=http_status,
            detail={"code": code, "message": f"test fixture: {code}"},
        )

    app.dependency_overrides[get_user_from_device_token_or_session] = _user_dep
    app.dependency_overrides[get_auth_context] = _ctx_dep
    return TestClient(app)
