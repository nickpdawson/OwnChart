"""Account-lockout enforcement (Batch C).

The security contract: disabling a user must freeze the account at the
auth boundary — the login endpoint refuses them (403), AND both live
credential classes stop working:

  - the cookie session (401), even a freshly-signed cookie whose
    ``stv`` still matches — the explicit ``disabled`` check is
    belt-and-suspenders on top of the session_token_version bump;
  - the device bearer token (401), left intact but refused.

Enabling reverses the freeze (control cases prove a non-disabled user
still authenticates through every path).

These exercise the real code paths (``core/device_auth.py`` +
``routes/auth.py`` login) with a fake session — no Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ownchart.core.device_auth import (
    _hash_token,
    get_user_from_device_token_or_session,
)
from ownchart.core.security import hash_password, sign_session
from ownchart.models.device_token import DeviceToken
from ownchart.models.user import User


# ---------------------------------------------------------------------------
# Fakes


class _R:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, execute_rows=None, get_map=None):
        self._rows = execute_rows or []
        self._get = get_map or {}
        self.commits = 0

    async def execute(self, *a, **k):
        return _R(self._rows)

    async def get(self, model, pk):
        return self._get.get(str(pk)) or self._get.get(pk)

    async def commit(self):
        self.commits += 1


def _user(*, disabled: bool, stv: int = 0) -> User:
    return User(
        id=uuid.uuid4(),
        email="tester@example.com",
        password_hash=hash_password("correct-horse-battery"),
        phi_consent_granted=True,
        is_instance_admin=False,
        session_token_version=stv,
        disabled=disabled,
        email_verified=True,
    )


def _bearer_request(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer " + token.encode())],
    }
    return Request(scope)


def _cookie_request(cookie_value: str) -> Request:
    from ownchart.core.config import get_settings

    name = get_settings().session_cookie_name
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"cookie", f"{name}={cookie_value}".encode())],
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Device bearer path


@pytest.mark.asyncio
async def test_bearer_token_of_disabled_user_is_401():
    user = _user(disabled=True)
    token = "a-real-device-token-value"
    dt = DeviceToken(
        id=uuid.uuid4(), user_id=user.id, name="iPhone",
        hashed_token=_hash_token(token),
    )
    db = _FakeDb(execute_rows=[dt], get_map={str(user.id): user})
    with pytest.raises(HTTPException) as exc:
        await get_user_from_device_token_or_session(_bearer_request(token), db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_bearer_token_of_enabled_user_authenticates():
    user = _user(disabled=False)
    token = "a-real-device-token-value"
    dt = DeviceToken(
        id=uuid.uuid4(), user_id=user.id, name="iPhone",
        hashed_token=_hash_token(token),
    )
    db = _FakeDb(execute_rows=[dt], get_map={str(user.id): user})
    got = await get_user_from_device_token_or_session(_bearer_request(token), db)
    assert got.id == user.id


# ---------------------------------------------------------------------------
# Cookie session path — belt-and-suspenders even when stv still matches


@pytest.mark.asyncio
async def test_cookie_session_of_disabled_user_is_401_even_with_matching_stv():
    # stv=0 on the user AND the cookie (no stv key → read as 0), so the
    # stv gate PASSES. Only the explicit disabled check should 401.
    user = _user(disabled=True, stv=0)
    cookie = sign_session({"uid": str(user.id)})
    db = _FakeDb(get_map={str(user.id): user})
    with pytest.raises(HTTPException) as exc:
        await get_user_from_device_token_or_session(_cookie_request(cookie), db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_cookie_session_of_enabled_user_authenticates():
    user = _user(disabled=False, stv=0)
    cookie = sign_session({"uid": str(user.id)})
    db = _FakeDb(get_map={str(user.id): user})
    got = await get_user_from_device_token_or_session(_cookie_request(cookie), db)
    assert got.id == user.id


# ---------------------------------------------------------------------------
# Login path


def _login_client(app, user: User):
    from ownchart.core.db import get_session

    db = _FakeDb(execute_rows=[user])

    async def _fake():
        yield db

    app.dependency_overrides[get_session] = _fake
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_login_of_disabled_user_is_403(app_fixture):
    user = _user(disabled=True)
    c = _login_client(app_fixture, user)
    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "correct-horse-battery"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "account_disabled"


def test_login_of_enabled_user_succeeds(app_fixture):
    user = _user(disabled=False)
    c = _login_client(app_fixture, user)
    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "correct-horse-battery"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == user.email


def test_login_wrong_password_still_401_not_403(app_fixture):
    # A disabled account must not be distinguishable from a wrong password
    # to an unauthenticated prober: wrong creds → 401 before the disabled
    # check is even reached.
    user = _user(disabled=True)
    c = _login_client(app_fixture, user)
    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "WRONG"},
    )
    assert r.status_code == 401, r.text
