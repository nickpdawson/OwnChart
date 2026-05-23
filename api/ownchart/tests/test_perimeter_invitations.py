"""Invitation route perimeter — FU-MULTITENANT-ONBOARDING.

Pins the auth gate on every invitation endpoint: when the
AuthContext dependency denies (record_access_revoked,
no_memberships, insufficient_role), the route returns 403 with
that code instead of silently 200-ing.

Follows the perimeter-rollout pattern from test_perimeter_sources_read.py.
"""

from __future__ import annotations

import uuid

from ownchart.tests.conftest import denied_client


def test_create_invite_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.post(
        "/api/invitations",
        json={
            "invited_email": "b@example.com",
            "target_kind": "existing_record",
            "target_person_record_id": str(uuid.uuid4()),
            "role": "caregiver",
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


def test_create_invite_403_on_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post(
        "/api/invitations",
        json={
            "invited_email": "b@example.com",
            "target_kind": "new_record",
            "role": "owner",
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_list_invitations_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.get("/api/invitations")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


def test_revoke_invitation_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.delete(f"/api/invitations/{uuid.uuid4()}")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


def test_create_invite_propagates_unauthenticated(app_fixture):
    from fastapi import HTTPException, status
    from fastapi.testclient import TestClient

    from ownchart.core.device_auth import get_user_from_device_token_or_session

    async def _raise_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    app_fixture.dependency_overrides[
        get_user_from_device_token_or_session
    ] = _raise_401
    c = TestClient(app_fixture)
    r = c.post(
        "/api/invitations",
        json={
            "invited_email": "b@example.com",
            "target_kind": "new_record",
            "role": "owner",
        },
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Preview is unauthenticated — pins that it does NOT depend on
# AuthContext or any user-fetcher. Structural assertion against
# FastAPI's resolved dependency graph; no DB needed.


def test_preview_route_has_no_auth_dependency(app_fixture):
    """The /preview route is the public landing for the accept
    page. It MUST NOT depend on the session/device-token user
    fetcher or on `get_auth_context` — otherwise an unauthenticated
    invitee couldn't read the invite they're about to accept.
    """
    from ownchart.core.auth_context import get_auth_context
    from ownchart.core.device_auth import get_user_from_device_token_or_session

    preview_route = next(
        (
            r for r in app_fixture.routes
            if getattr(r, "path", None) == "/api/invitations/preview"
        ),
        None,
    )
    assert preview_route is not None, "preview route not registered"
    # Recursively walk the route's dependency graph.
    seen_deps: list = []
    stack = list(preview_route.dependant.dependencies)
    while stack:
        d = stack.pop()
        seen_deps.append(d.call)
        stack.extend(d.dependencies)
    assert get_auth_context not in seen_deps, (
        "preview route must not depend on get_auth_context"
    )
    assert get_user_from_device_token_or_session not in seen_deps, (
        "preview route must not depend on user-fetcher"
    )
