"""Email/SMTP workstream — password reset.

Two layers, matching the codebase's classifier-plus-route-contract pattern:

  1. Pure ``classify_reset_token`` / ``_humanize_ttl`` — no DB.
  2. Route contracts for ``/password/reset-request`` (always 200,
     anti-enumeration, rate-limited) and ``/password/reset`` (consumes the
     token, changes the password, invalidates other sessions; rejects
     expired / consumed / invalid / weak / unchanged) with the thin DB
     helpers patched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ownchart.models.password_reset_token import PasswordResetToken
from ownchart.routes import auth_recovery as rec


# ---------------------------------------------------------------------------
# Fakes


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, get_map=None, execute_rows=None):
        self._get_map = get_map or {}
        self._execute_rows = execute_rows or []
        self.added = []
        self.commits = 0
        self.executed = []  # record statements for assertions

    async def execute(self, statement=None, *a, **k):
        self.executed.append(statement)
        return _FakeResult(self._execute_rows)

    async def get(self, model, pk):
        return self._get_map.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    async def refresh(self, obj):
        pass


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _active_token(user_id):
    now = datetime.now(timezone.utc)
    return PasswordResetToken(
        user_id=user_id,
        token_hash="argon2-hash",
        token_lookup_prefix="prefix12",
        expires_at=now + timedelta(hours=1),
        created_at=now,
    )


def _user(email="user@example.com", pw_hash="OLDHASH", stv=0):
    from ownchart.models.user import User
    return User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pw_hash,
        session_token_version=stv,
        default_person_record_id=None,
    )


# ---------------------------------------------------------------------------
# 1. Pure classifier / humanizer


def test_classify_reset_token_states():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    assert rec.classify_reset_token(None, now=now) == "not_found"

    active = PasswordResetToken(
        user_id=uuid.uuid4(), token_hash="h", token_lookup_prefix="p",
        expires_at=now + timedelta(hours=1), created_at=now,
    )
    assert rec.classify_reset_token(active, now=now) == "ok"

    expired = PasswordResetToken(
        user_id=uuid.uuid4(), token_hash="h", token_lookup_prefix="p",
        expires_at=now - timedelta(seconds=1), created_at=now,
    )
    assert rec.classify_reset_token(expired, now=now) == "expired"

    consumed = PasswordResetToken(
        user_id=uuid.uuid4(), token_hash="h", token_lookup_prefix="p",
        expires_at=now + timedelta(hours=1), created_at=now,
        consumed_at=now,
    )
    assert rec.classify_reset_token(consumed, now=now) == "consumed"


def test_humanize_ttl_phrases():
    assert rec._humanize_ttl(3600) == "This link expires in about 1 hour."
    assert rec._humanize_ttl(2 * 3600) == "This link expires in about 2 hours."
    assert rec._humanize_ttl(86400) == "This link expires in 1 day."
    assert rec._humanize_ttl(7 * 86400) == "This link expires in 7 days."
    assert rec._humanize_ttl(300) == "This link expires in about 5 minutes."


# ---------------------------------------------------------------------------
# 2a. reset-request — anti-enumeration


def test_reset_request_returns_200_for_existing_and_missing(app_fixture, monkeypatch):
    issued = []

    async def _fake_find(db, email):
        return _user(email=email) if email == "known@example.com" else None

    async def _fake_issue(db, user):
        issued.append(user.email)

    monkeypatch.setattr(rec, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(rec, "_find_user_by_email", _fake_find)
    monkeypatch.setattr(rec, "_issue_and_send_reset", _fake_issue)
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)

    r_known = c.post(
        "/api/auth/password/reset-request",
        json={"email": "known@example.com"},
        headers={"x-forwarded-for": "198.51.100.10"},
    )
    r_unknown = c.post(
        "/api/auth/password/reset-request",
        json={"email": "nobody@example.com"},
        headers={"x-forwarded-for": "198.51.100.11"},
    )
    assert r_known.status_code == 200, r_known.text
    assert r_unknown.status_code == 200, r_unknown.text
    assert r_known.json() == {"ok": True}
    assert r_unknown.json() == {"ok": True}
    # A token was minted ONLY for the real user.
    assert issued == ["known@example.com"]


def test_reset_request_noop_when_smtp_unconfigured(app_fixture, monkeypatch):
    issued = []

    async def _fake_find(db, email):
        return _user(email=email)

    async def _fake_issue(db, user):
        issued.append(user.email)

    monkeypatch.setattr(rec, "is_smtp_configured", lambda: False)
    monkeypatch.setattr(rec, "_find_user_by_email", _fake_find)
    monkeypatch.setattr(rec, "_issue_and_send_reset", _fake_issue)
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset-request",
        json={"email": "known@example.com"},
        headers={"x-forwarded-for": "198.51.100.12"},
    )
    assert r.status_code == 200
    # No DB work at all when the instance can't send mail.
    assert issued == []


def test_reset_request_is_rate_limited(app_fixture, monkeypatch):
    async def _fake_find(db, email):
        return None

    monkeypatch.setattr(rec, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(rec, "_find_user_by_email", _fake_find)
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)

    ip = "203.0.113.200"
    codes = []
    # Default auth limit is 10 per window; the 11th trips.
    for _ in range(12):
        r = c.post(
            "/api/auth/password/reset-request",
            json={"email": "spray@example.com"},
            headers={"x-forwarded-for": ip},
        )
        codes.append(r.status_code)
    assert codes[:10] == [200] * 10
    assert 429 in codes[10:]


# ---------------------------------------------------------------------------
# 2b. reset — consume + rotate


def test_reset_consumes_token_and_rotates_password(app_fixture, monkeypatch):
    user = _user(pw_hash="OLDHASH", stv=3)
    token = _active_token(user.id)

    async def _fake_lookup(db, raw):
        return token

    monkeypatch.setattr(rec, "_lookup_reset_token", _fake_lookup)
    monkeypatch.setattr(rec, "hash_password", lambda p: "NEWHASH")
    # New password differs from the old one.
    monkeypatch.setattr(rec, "verify_password", lambda p, h: False)
    session = _FakeSession(get_map={user.id: user})
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset",
        json={"token": "a-valid-looking-token", "new_password": "brandnewpw"},
        headers={"x-forwarded-for": "198.51.100.20"},
    )
    assert r.status_code == 200, r.text
    assert user.password_hash == "NEWHASH"
    # Other web sessions invalidated.
    assert user.session_token_version == 4
    # Token single-use enforced.
    assert token.consumed_at is not None
    assert session.commits == 1
    # P1-10: the reset also revokes the user's device bearer tokens in the
    # same transaction (a stolen device token must not survive a recovery).
    assert any(
        "device_tokens" in str(stmt).lower() for stmt in session.executed
    ), session.executed


def test_reset_rejects_expired_token(app_fixture, monkeypatch):
    user = _user()
    now = datetime.now(timezone.utc)
    expired = PasswordResetToken(
        user_id=user.id, token_hash="h", token_lookup_prefix="p",
        expires_at=now - timedelta(minutes=1), created_at=now,
    )

    async def _fake_lookup(db, raw):
        return expired

    monkeypatch.setattr(rec, "_lookup_reset_token", _fake_lookup)
    _override_session(app_fixture, _FakeSession(get_map={user.id: user}))
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset",
        json={"token": "a-valid-looking-token", "new_password": "brandnewpw"},
        headers={"x-forwarded-for": "198.51.100.21"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_or_expired_token"


def test_reset_rejects_unknown_token(app_fixture, monkeypatch):
    async def _fake_lookup(db, raw):
        return None

    monkeypatch.setattr(rec, "_lookup_reset_token", _fake_lookup)
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset",
        json={"token": "totally-bogus-token", "new_password": "brandnewpw"},
        headers={"x-forwarded-for": "198.51.100.22"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_or_expired_token"


def test_reset_rejects_weak_password(app_fixture, monkeypatch):
    # Weak-password guard fires before any token lookup.
    called = {"n": 0}

    async def _fake_lookup(db, raw):
        called["n"] += 1
        return None

    monkeypatch.setattr(rec, "_lookup_reset_token", _fake_lookup)
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset",
        json={"token": "a-valid-looking-token", "new_password": "short"},
        headers={"x-forwarded-for": "198.51.100.23"},
    )
    assert r.status_code == 422
    # P1-9: unified policy code across every set-password site.
    assert r.json()["detail"]["code"] == "password_policy"
    assert called["n"] == 0


def test_reset_rejects_unchanged_password(app_fixture, monkeypatch):
    user = _user(pw_hash="OLDHASH")
    token = _active_token(user.id)

    async def _fake_lookup(db, raw):
        return token

    monkeypatch.setattr(rec, "_lookup_reset_token", _fake_lookup)
    # New password matches the existing hash → reject.
    monkeypatch.setattr(rec, "verify_password", lambda p, h: True)
    session = _FakeSession(get_map={user.id: user})
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/password/reset",
        json={"token": "a-valid-looking-token", "new_password": "samepasswd"},
        headers={"x-forwarded-for": "198.51.100.24"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "password_unchanged"
    # Nothing consumed / committed on a rejected reset.
    assert token.consumed_at is None
    assert session.commits == 0
