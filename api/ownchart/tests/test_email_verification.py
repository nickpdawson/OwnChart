"""Email/SMTP workstream — email verification.

Covers:
  - The signed verification-token round-trip / tamper / expiry / email-bind.
  - ``POST /api/auth/verify-email`` flips the flag and is idempotent.
  - Login enforcement: with the flag on (and SMTP configured) an unverified
    user is blocked; verified users pass; with the flag off nothing changes.
  - Registration: flag on ⇒ unverified + no auto-login + verification email;
    flag off ⇒ verified + logged in.
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from ownchart.core import security
from ownchart.core.app_config import AppConfig, AuthConfig, InstanceConfig
from ownchart.routes import auth as auth_routes


# ---------------------------------------------------------------------------
# Fakes (mirror test_password_reset)


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

    async def execute(self, *a, **k):
        return _FakeResult(self._execute_rows)

    async def get(self, model, pk):
        return self._get_map.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        # Real Postgres applies the model's server/column defaults at INSERT.
        # Mirror the one the register response reads so the fake round-trips.
        for obj in self.added:
            if getattr(obj, "phi_consent_granted", None) is None:
                obj.phi_consent_granted = False

    async def refresh(self, obj):
        pass


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _user(email="user@example.com", verified=False):
    from ownchart.models.user import User
    return User(
        id=uuid.uuid4(),
        email=email,
        password_hash="OLDHASH",
        session_token_version=0,
        email_verified=verified,
        phi_consent_granted=False,
        is_instance_admin=False,
        default_person_record_id=None,
    )


def _cfg(*, require_verification=False, allow_self_reg=False):
    return AppConfig(
        instance=InstanceConfig(name="OwnChart"),
        auth=AuthConfig(
            allow_self_registration=allow_self_reg,
            require_email_verification=require_verification,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Signed-token helper


def test_verification_token_round_trips():
    uid = str(uuid.uuid4())
    tok = security.make_email_verification_token(uid, "a@b.com")
    payload = security.read_email_verification_token(tok, max_age=3600)
    assert payload == {"uid": uid, "email": "a@b.com"}


def test_verification_token_expired_returns_none():
    tok = security.make_email_verification_token(str(uuid.uuid4()), "a@b.com")
    # max_age in the past → treated as expired.
    time.sleep(1)
    assert security.read_email_verification_token(tok, max_age=0) is None


def test_verification_token_tamper_returns_none():
    tok = security.make_email_verification_token(str(uuid.uuid4()), "a@b.com")
    assert security.read_email_verification_token(tok + "x", max_age=3600) is None
    assert security.read_email_verification_token("garbage", max_age=3600) is None


# ---------------------------------------------------------------------------
# 2. verify-email route


def test_verify_email_flips_flag(app_fixture):
    user = _user(email="v@example.com", verified=False)
    tok = security.make_email_verification_token(str(user.id), user.email)
    session = _FakeSession(get_map={user.id: user})
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/verify-email", json={"token": tok},
        headers={"x-forwarded-for": "198.51.100.30"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True
    assert user.email_verified is True
    assert user.email_verified_at is not None
    assert session.commits == 1


def test_verify_email_idempotent_when_already_verified(app_fixture):
    user = _user(email="v@example.com", verified=True)
    tok = security.make_email_verification_token(str(user.id), user.email)
    session = _FakeSession(get_map={user.id: user})
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/verify-email", json={"token": tok},
        headers={"x-forwarded-for": "198.51.100.31"},
    )
    assert r.status_code == 200
    # No second write.
    assert session.commits == 0


def test_verify_email_rejects_bad_token(app_fixture):
    _override_session(app_fixture, _FakeSession())
    c = TestClient(app_fixture)
    r = c.post(
        "/api/auth/verify-email", json={"token": "not-a-real-token"},
        headers={"x-forwarded-for": "198.51.100.32"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_or_expired_token"


def test_verify_email_rejects_email_mismatch(app_fixture):
    # Token was minted for one email; the account's email has since changed.
    user = _user(email="new@example.com", verified=False)
    tok = security.make_email_verification_token(str(user.id), "old@example.com")
    session = _FakeSession(get_map={user.id: user})
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/verify-email", json={"token": tok},
        headers={"x-forwarded-for": "198.51.100.33"},
    )
    assert r.status_code == 400
    assert user.email_verified is False


# ---------------------------------------------------------------------------
# 3. Login enforcement


def _patch_login(monkeypatch, *, cfg, smtp_configured, password_ok=True):
    monkeypatch.setattr(auth_routes, "get_app_config", lambda: cfg)
    monkeypatch.setattr(auth_routes, "verify_password", lambda p, h: password_ok)
    from ownchart.core import email as email_mod
    monkeypatch.setattr(email_mod, "is_smtp_configured", lambda: smtp_configured)


def test_login_blocked_when_unverified_and_flag_on(app_fixture, monkeypatch):
    user = _user(verified=False)
    _override_session(app_fixture, _FakeSession(execute_rows=[user]))
    _patch_login(monkeypatch, cfg=_cfg(require_verification=True), smtp_configured=True)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "whatever"},
        headers={"x-forwarded-for": "198.51.100.40"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "email_not_verified"


def test_login_allowed_when_verified_and_flag_on(app_fixture, monkeypatch):
    user = _user(verified=True)
    _override_session(app_fixture, _FakeSession(execute_rows=[user]))
    _patch_login(monkeypatch, cfg=_cfg(require_verification=True), smtp_configured=True)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "whatever"},
        headers={"x-forwarded-for": "198.51.100.41"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True


def test_login_unaffected_when_flag_off(app_fixture, monkeypatch):
    # Flag off: an unverified user still logs in fine (self-host default).
    user = _user(verified=False)
    _override_session(app_fixture, _FakeSession(execute_rows=[user]))
    _patch_login(monkeypatch, cfg=_cfg(require_verification=False), smtp_configured=True)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/login",
        json={"email": user.email, "password": "whatever"},
        headers={"x-forwarded-for": "198.51.100.42"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 4. Registration behavior


def _patch_register(monkeypatch, *, cfg, smtp_configured, send_recorder=None):
    monkeypatch.setattr(auth_routes, "get_app_config", lambda: cfg)
    monkeypatch.setattr(auth_routes, "hash_password", lambda p: "HASH")
    from ownchart.core import email as email_mod
    monkeypatch.setattr(email_mod, "is_smtp_configured", lambda: smtp_configured)
    if send_recorder is not None:
        monkeypatch.setattr(email_mod, "send_email_verification_email", send_recorder)


def test_register_flag_on_unverified_no_login_sends_email(app_fixture, monkeypatch):
    sent = []

    async def _fake_send(*, to, verify_url, expiry_note):
        sent.append(to)
        return True

    # A pre-existing user so the new registration is NOT the first user.
    existing = _user(email="first@example.com", verified=True)
    _override_session(app_fixture, _FakeSession(execute_rows=[existing]))
    _patch_register(
        monkeypatch,
        cfg=_cfg(require_verification=True, allow_self_reg=True),
        smtp_configured=True,
        send_recorder=_fake_send,
    )
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={"email": "newbie@example.com", "password": "longenough"},
        headers={"x-forwarded-for": "198.51.100.50"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email_verified"] is False
    # Not auto-logged-in — no session cookie.
    assert "ownchart_session" not in r.headers.get("set-cookie", "")
    # Verification email was dispatched.
    assert sent == ["newbie@example.com"]


def test_register_flag_off_verified_and_logged_in(app_fixture, monkeypatch):
    existing = _user(email="first@example.com", verified=True)
    _override_session(app_fixture, _FakeSession(execute_rows=[existing]))
    _patch_register(
        monkeypatch,
        cfg=_cfg(require_verification=False, allow_self_reg=True),
        smtp_configured=True,
    )
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={"email": "newbie2@example.com", "password": "longenough"},
        headers={"x-forwarded-for": "198.51.100.51"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email_verified"] is True
    # Auto-logged-in — session cookie present.
    assert "ownchart_session" in r.headers.get("set-cookie", "")
