"""LINK-01 — POST /api/invitations/preview (token in body, not query).

The invite token used to ride in the `GET .../preview?token=...` query
string, where a reverse proxy / web server captures it in access logs.
LINK-01 moves it into the JSON body. These tests exercise the POST-body
variant:

  - happy path returns the invite shape and never echoes the token;
  - expired / consumed / revoked / unknown → a uniform 410 that doesn't
    leak which terminal state the invite is in;
  - GET and POST share the SAME per-IP `invite_preview` rate-limit bucket;
  - the raw token appears in no log output.

Route DB is faked (same pattern as test_invite_authed_accept). The token
is real — `verify_invite_token` runs for true — so the lookup path is
exercised end to end without a Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ownchart.core.security import (
    generate_invite_token,
    hash_invite_token,
    invite_lookup_prefix,
)
from ownchart.models.invitation import Invitation
from ownchart.models.person_record import PersonRecord

# ---------------------------------------------------------------------------
# Fakes — a session that returns preset candidate rows and records.


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, get_map=None, execute_rows=None):
        self._get_map = get_map or {}
        self._execute_rows = execute_rows or []

    async def execute(self, *a, **k):
        return _FakeResult(self._execute_rows)

    async def get(self, model, pk):
        return self._get_map.get(pk)


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _client(app, session):
    from fastapi.testclient import TestClient

    _override_session(app, session)
    return TestClient(app)


def _invite(
    token: str,
    *,
    create_new_record: bool = True,
    target_id=None,
    role: str = "owner",
    proposed_record_name: str | None = "New Person",
    expires_at: datetime | None = None,
    accepted_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Invitation:
    now = datetime.now(timezone.utc)
    return Invitation(
        id=uuid.uuid4(),
        invited_email="b@example.com",
        target_person_record_id=target_id,
        create_new_record=create_new_record,
        proposed_record_name=proposed_record_name,
        role=role,
        token_hash=hash_invite_token(token),
        token_lookup_prefix=invite_lookup_prefix(token),
        expires_at=expires_at or (now + timedelta(days=1)),
        created_by_user_id=uuid.uuid4(),
        created_at=now,
        accepted_at=accepted_at,
        accepted_by_user_id=None,
        revoked_at=revoked_at,
    )


def _record(rid, name="Mom"):
    now = datetime.now(timezone.utc)
    return PersonRecord(
        id=rid,
        display_name=name,
        is_self=False,
        created_by_user_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


# A unique X-Forwarded-For per call keeps each test in its own rate-limit
# bucket so the module-singleton limiter can't bleed counts across tests.
def _ip():
    return {"X-Forwarded-For": f"198.51.100.{uuid.uuid4().int % 250 + 1}"}


_UNIFORM_410 = {
    "code": "invitation_unavailable",
    "message": "This invite is no longer available.",
}


# ---------------------------------------------------------------------------
# Happy path


def test_post_preview_happy_new_record(app_fixture):
    token = generate_invite_token()
    invite = _invite(token, create_new_record=True, role="owner")
    c = _client(app_fixture, _FakeSession(execute_rows=[invite]))

    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "invited_email": "b@example.com",
        "role": "owner",
        "target_kind": "new_record",
        "target_display_name": None,
        "proposed_record_name": "New Person",
        "expires_at": body["expires_at"],  # shape asserted below
    }
    assert body["expires_at"]  # present
    # Never echo the token back.
    assert token not in r.text


def test_post_preview_happy_existing_record_shows_display_name(app_fixture):
    token = generate_invite_token()
    rid = uuid.uuid4()
    invite = _invite(
        token, create_new_record=False, target_id=rid, role="caregiver",
        proposed_record_name=None,
    )
    c = _client(
        app_fixture,
        _FakeSession(get_map={rid: _record(rid, "Mom")}, execute_rows=[invite]),
    )

    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_kind"] == "existing_record"
    assert body["target_display_name"] == "Mom"
    assert body["role"] == "caregiver"
    assert token not in r.text


# ---------------------------------------------------------------------------
# Terminal states → uniform 410 (no state leak)


def _assert_uniform_410(r, token):
    assert r.status_code == 410, r.text
    assert r.json()["detail"] == _UNIFORM_410
    assert token not in r.text


def test_post_preview_expired_410(app_fixture):
    token = generate_invite_token()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    invite = _invite(token, expires_at=past)
    c = _client(app_fixture, _FakeSession(execute_rows=[invite]))
    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())
    _assert_uniform_410(r, token)


def test_post_preview_consumed_410(app_fixture):
    token = generate_invite_token()
    invite = _invite(token, accepted_at=datetime.now(timezone.utc))
    c = _client(app_fixture, _FakeSession(execute_rows=[invite]))
    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())
    _assert_uniform_410(r, token)


def test_post_preview_revoked_410(app_fixture):
    token = generate_invite_token()
    invite = _invite(token, revoked_at=datetime.now(timezone.utc))
    c = _client(app_fixture, _FakeSession(execute_rows=[invite]))
    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())
    _assert_uniform_410(r, token)


def test_post_preview_unknown_token_410(app_fixture):
    token = generate_invite_token()
    # No candidates match the lookup prefix.
    c = _client(app_fixture, _FakeSession(execute_rows=[]))
    r = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())
    _assert_uniform_410(r, token)


def test_post_preview_short_token_410_without_db_hit(app_fixture):
    # Too-short tokens are rejected before any lookup — same uniform 410.
    c = _client(app_fixture, _FakeSession(execute_rows=[]))
    r = c.post("/api/invitations/preview", json={"token": "short"}, headers=_ip())
    _assert_uniform_410(r, "short")


def test_post_preview_missing_token_422(app_fixture):
    # `token` is required by the Pydantic model.
    c = _client(app_fixture, _FakeSession(execute_rows=[]))
    r = c.post("/api/invitations/preview", json={}, headers=_ip())
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# GET and POST share the SAME per-IP rate-limit bucket


def test_get_and_post_preview_share_rate_limit_bucket(app_fixture, monkeypatch):
    from ownchart.core.config import get_settings

    # Tighten the cap so we can cross it in a few calls. The route reads
    # this off the cached Settings singleton; monkeypatch restores it.
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_invite_preview_max_attempts", 3)

    c = _client(app_fixture, _FakeSession(execute_rows=[]))
    ip = {"X-Forwarded-For": "203.0.113.77"}  # one fixed IP across the run
    tok = generate_invite_token()

    # 3 allowed attempts, split across GET and POST → they must draw from
    # the same (bucket, IP) counter.
    r1 = c.get("/api/invitations/preview", params={"token": tok}, headers=ip)
    r2 = c.post("/api/invitations/preview", json={"token": tok}, headers=ip)
    r3 = c.get("/api/invitations/preview", params={"token": tok}, headers=ip)
    assert [r1.status_code, r2.status_code, r3.status_code] == [410, 410, 410], (
        r1.text, r2.text, r3.text,
    )

    # The 4th attempt (a POST) is blocked because GET calls already spent
    # the shared budget. If GET/POST used separate buckets, POST's own
    # count would be 2 here and this would be a 410, not a 429.
    r4 = c.post("/api/invitations/preview", json={"token": tok}, headers=ip)
    assert r4.status_code == 429, r4.text
    assert r4.json()["detail"]["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# Token never appears in log output


def test_post_preview_does_not_log_token(app_fixture, capsys):
    token = generate_invite_token()
    invite = _invite(token, create_new_record=True)
    c = _client(app_fixture, _FakeSession(execute_rows=[invite]))

    # Drain anything already buffered, then exercise happy + terminal paths.
    capsys.readouterr()
    ok = c.post("/api/invitations/preview", json={"token": token}, headers=_ip())
    assert ok.status_code == 200, ok.text

    gone_token = generate_invite_token()
    gone = _invite(gone_token, revoked_at=datetime.now(timezone.utc))
    c2 = _client(app_fixture, _FakeSession(execute_rows=[gone]))
    r = c2.post(
        "/api/invitations/preview", json={"token": gone_token}, headers=_ip(),
    )
    assert r.status_code == 410, r.text

    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert gone_token not in captured.out
    assert gone_token not in captured.err


# ---------------------------------------------------------------------------
# Structural — neither preview route depends on auth


def test_both_preview_routes_have_no_auth_dependency(app_fixture):
    from ownchart.core.auth_context import get_auth_context
    from ownchart.core.device_auth import (
        get_user_from_device_token_or_session,
    )

    preview_routes = [
        r for r in app_fixture.routes
        if getattr(r, "path", None) == "/api/invitations/preview"
    ]
    # Both GET and POST must be registered.
    methods = set()
    for pr in preview_routes:
        methods |= set(getattr(pr, "methods", set()) or set())
    assert {"GET", "POST"} <= methods, methods

    for pr in preview_routes:
        seen = []
        stack = list(pr.dependant.dependencies)
        while stack:
            d = stack.pop()
            seen.append(d.call)
            stack.extend(d.dependencies)
        assert get_auth_context not in seen, pr.methods
        assert get_user_from_device_token_or_session not in seen, pr.methods
