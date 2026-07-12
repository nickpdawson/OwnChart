"""Auto Export token auth + config gating (CRIT-2 / CRIT-3).

Three defects this guards:
  - CRIT-2: GET /config returned the deployment write-token to ANY
    membership, incl. `viewer` (viewer→writer escalation). Now
    owner-only and the raw token is never returned.
  - CRIT-3a: per-record token create route now exists (owner-only).
  - CRIT-3b: the token `scopes` column is now enforced on push.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from ownchart.core.auto_export_auth import (
    PUSH_SCOPE,
    authenticate_auto_export_push,
)
from ownchart.models.auto_export_token import AutoExportToken

from .conftest import authed_client


# ---------------------------------------------------------------------------
# CRIT-2 — GET /config role gate + no raw token in the response


def test_config_viewer_forbidden_and_no_token_leak(app_fixture):
    client = authed_client(app_fixture, role="viewer")
    resp = client.get("/api/auto-export/config")
    assert resp.status_code == 403
    # The escalation was that a viewer could read the write-token. Make
    # sure no field named "token" carrying a value slips through.
    assert "token" not in resp.json()


def test_config_owner_allowed_returns_no_raw_token(app_fixture):
    client = authed_client(app_fixture, role="owner")
    resp = client.get("/api/auto-export/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "configured" in body
    assert "token_hint" in body
    # The raw deployment token is never returned anymore.
    assert "token" not in body


def test_create_token_requires_owner(app_fixture):
    """A caregiver (below owner) cannot mint a push token. The role gate
    runs before the route body, so this doesn't touch the DB."""
    client = authed_client(app_fixture, role="caregiver")
    resp = client.post("/api/auto-export/tokens", json={"label": "iPhone"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# CRIT-3b — scope enforcement on push (mocked DB; no Postgres needed)


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    """Minimal async DB stand-in for the token path of
    authenticate_auto_export_push."""

    def __init__(self, token_row, user):
        self._token = token_row
        self._user = user
        self.execute_calls = 0

    async def execute(self, _stmt):
        # First call = the token SELECT; later calls (last_used_at
        # UPDATE) return an ignored result.
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _Result(self._token)
        return _Result(None)

    async def get(self, _model, _pk):
        return self._user


def _token(scopes):
    return AutoExportToken(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
        label="iPhone",
        token_hash="deadbeef",
        scopes=scopes,
    )


@pytest.mark.asyncio
async def test_push_token_with_push_scope_authenticates():
    tok = _token([PUSH_SCOPE])

    class _User:
        pass

    user = _User()
    user.id = tok.user_id
    db = _FakeDB(tok, user)
    result = await authenticate_auto_export_push(
        db, authorization_header="Bearer whatever",
    )
    assert result.auth_method == "token"
    assert result.person_record_id == tok.person_record_id


@pytest.mark.asyncio
async def test_push_token_without_push_scope_rejected():
    tok = _token(["read_only"])  # no 'push'
    db = _FakeDB(tok, object())
    with pytest.raises(HTTPException) as exc:
        await authenticate_auto_export_push(
            db, authorization_header="Bearer whatever",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_push_token_empty_scopes_rejected():
    tok = _token([])
    db = _FakeDB(tok, object())
    with pytest.raises(HTTPException) as exc:
        await authenticate_auto_export_push(
            db, authorization_header="Bearer whatever",
        )
    assert exc.value.status_code == 403
