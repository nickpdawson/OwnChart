"""First-owner bootstrap setup-token gate (P0-1 release review).

Covers:
  1. Token module — env pin vs file source of truth, constant-time
     compare, provision/clear lifecycle.
  2. Advisory-lock helper — issues the Postgres lock only on Postgres.
  3. Route contract — the empty-DB register path now requires the setup
     token (missing → setup_token_required; wrong → setup_token_invalid;
     correct → owner created AND token file removed); the token is never
     consulted once a user exists.

Mirrors the FakeSession + TestClient + monkeypatch style used by
test_email_verification / test_invite_authed_accept. A real-Postgres
concurrency test is out of scope for the pure-function suite (no live DB
in the runner); the advisory lock is exercised structurally in §2 and the
serialization guarantee is documented in _serialize_bootstrap.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ownchart.core import setup_token as st
from ownchart.core.app_config import AppConfig, AuthConfig, InstanceConfig
from ownchart.models.base import new_uuid
from ownchart.routes import auth as auth_routes


# ---------------------------------------------------------------------------
# Shared fakes


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0] if self._rows else 0

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """No get_bind() — so _serialize_bootstrap treats it as non-Postgres
    and no-ops, exactly like the real SQLite/mock degradation path."""

    def __init__(self, execute_rows=None):
        self._execute_rows = execute_rows or []
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _FakeResult(self._execute_rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        # Real Postgres applies column defaults at INSERT. Mirror the two
        # the register bootstrap path reads back: primary-key ids and the
        # phi_consent_granted default.
        for obj in self.added:
            if hasattr(obj, "id") and getattr(obj, "id", None) is None:
                obj.id = new_uuid()
            if getattr(obj, "phi_consent_granted", None) is None:
                obj.phi_consent_granted = False

    async def refresh(self, obj):
        pass


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _cfg(*, allow_self_reg=False):
    return AppConfig(
        instance=InstanceConfig(name="OwnChart"),
        auth=AuthConfig(allow_self_registration=allow_self_reg),
    )


def _use_tmp_data_dir(monkeypatch, tmp_path):
    """Point the token module at a throwaway data dir and clear any env pin
    so the file is the source of truth for the test."""
    monkeypatch.setattr(
        st, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.delenv(st._ENV_PIN, raising=False)


def _user(email="first@example.com"):
    from ownchart.models.user import User
    return User(
        id=uuid.uuid4(),
        email=email,
        password_hash="OLDHASH",
        session_token_version=0,
        email_verified=True,
        phi_consent_granted=False,
        is_instance_admin=True,
        default_person_record_id=None,
    )


# ---------------------------------------------------------------------------
# 1. Token module


def test_env_pin_wins_over_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        st, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path),
    )
    (tmp_path / "setup_token").write_text("FILE_TOKEN")
    monkeypatch.setenv(st._ENV_PIN, "ENV_TOKEN")
    assert st.active_setup_token() == "ENV_TOKEN"


def test_file_is_source_when_no_env_pin(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("FILE_TOKEN\n")
    assert st.active_setup_token() == "FILE_TOKEN"


def test_active_token_none_when_absent(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    assert st.active_setup_token() is None


def test_verify_setup_token_uses_constant_time_compare(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("CORRECT")

    calls = []
    real = st.secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(st.secrets, "compare_digest", _spy)

    assert st.verify_setup_token("CORRECT") is True
    assert st.verify_setup_token("WRONG") is False
    # Both real comparisons went through secrets.compare_digest.
    assert ("CORRECT", "CORRECT") in calls
    assert ("WRONG", "CORRECT") in calls


def test_verify_false_when_no_token_provided_or_active(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    # No active token at all.
    assert st.verify_setup_token("anything") is False
    assert st.verify_setup_token(None) is False
    # Active token present but nothing provided.
    (tmp_path / "setup_token").write_text("CORRECT")
    assert st.verify_setup_token(None) is False
    assert st.verify_setup_token("") is False


def test_provision_zero_users_writes_and_logs(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    st.provision_setup_token_if_needed(has_users=False)
    token_file = tmp_path / "setup_token"
    assert token_file.exists()
    body = token_file.read_text().strip()
    assert len(body) >= 32  # ~256 bits url-safe
    # File is owner-only (0600).
    assert (token_file.stat().st_mode & 0o777) == 0o600
    # And that token verifies.
    assert st.verify_setup_token(body) is True


def test_provision_adopts_env_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        st, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setenv(st._ENV_PIN, "PINNED_TOKEN")
    st.provision_setup_token_if_needed(has_users=False)
    assert (tmp_path / "setup_token").read_text().strip() == "PINNED_TOKEN"


def test_provision_clears_stale_file_when_users_exist(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("STALE")
    st.provision_setup_token_if_needed(has_users=True)
    assert not (tmp_path / "setup_token").exists()


def test_clear_setup_token_removes_file(monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("X")
    st.clear_setup_token()
    assert not (tmp_path / "setup_token").exists()
    # Idempotent — clearing again is a no-op, not an error.
    st.clear_setup_token()


# ---------------------------------------------------------------------------
# 2. Advisory-lock helper


class _PgBind:
    dialect = SimpleNamespace(name="postgresql")


class _SqliteBind:
    dialect = SimpleNamespace(name="sqlite")


class _LockRecordingSession:
    def __init__(self, bind):
        self._bind = bind
        self.executed = []

    def get_bind(self):
        return self._bind

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult([])


async def test_serialize_bootstrap_locks_on_postgres():
    s = _LockRecordingSession(_PgBind())
    await auth_routes._serialize_bootstrap(s)
    assert len(s.executed) == 1
    sql, params = s.executed[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == {"key": auth_routes._BOOTSTRAP_ADVISORY_LOCK_KEY}


async def test_serialize_bootstrap_noop_off_postgres():
    s = _LockRecordingSession(_SqliteBind())
    await auth_routes._serialize_bootstrap(s)
    assert s.executed == []


async def test_serialize_bootstrap_noop_without_bind():
    # The FakeSession has no get_bind(); the helper must swallow that and
    # skip rather than blow up registration on a mocked DB.
    await auth_routes._serialize_bootstrap(_FakeSession())


# ---------------------------------------------------------------------------
# 3. Route contract


def _patch_register(monkeypatch, *, cfg):
    monkeypatch.setattr(auth_routes, "get_app_config", lambda: cfg)
    monkeypatch.setattr(auth_routes, "hash_password", lambda p: "HASH")
    from ownchart.core import email as email_mod
    monkeypatch.setattr(email_mod, "is_smtp_configured", lambda: False)


def test_bootstrap_no_token_403_required(app_fixture, monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("GOODTOKEN")
    _patch_register(monkeypatch, cfg=_cfg())
    session = _FakeSession(execute_rows=[])  # zero users
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={"email": "owner@example.com", "password": "longenough"},
        headers={"x-forwarded-for": "198.51.100.60"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "setup_token_required"
    # Nothing was created.
    assert session.commits == 0
    # Token still on disk — bootstrap is still possible.
    assert (tmp_path / "setup_token").exists()


def test_bootstrap_wrong_token_403_invalid(app_fixture, monkeypatch, tmp_path):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("GOODTOKEN")
    _patch_register(monkeypatch, cfg=_cfg())
    session = _FakeSession(execute_rows=[])
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={
            "email": "owner@example.com",
            "password": "longenough",
            "setup_token": "NOPE",
        },
        headers={"x-forwarded-for": "198.51.100.61"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "setup_token_invalid"
    assert session.commits == 0
    assert (tmp_path / "setup_token").exists()


def test_bootstrap_correct_token_creates_admin_and_removes_file(
    app_fixture, monkeypatch, tmp_path,
):
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("GOODTOKEN")
    _patch_register(monkeypatch, cfg=_cfg())
    session = _FakeSession(execute_rows=[])  # zero users
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={
            "email": "owner@example.com",
            "password": "longenough",
            "setup_token": "GOODTOKEN",
        },
        headers={"x-forwarded-for": "198.51.100.62"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_instance_admin"] is True
    assert body["email"] == "owner@example.com"
    assert session.commits == 1
    # Token consumed — the bootstrap path is now closed.
    assert not (tmp_path / "setup_token").exists()
    # Owner is auto-logged-in.
    assert "ownchart_session" in r.headers.get("set-cookie", "")


def test_token_not_required_once_a_user_exists(app_fixture, monkeypatch, tmp_path):
    # A leftover token file must NOT let a second registrant become admin,
    # and no token is needed on the subsequent-user path.
    _use_tmp_data_dir(monkeypatch, tmp_path)
    (tmp_path / "setup_token").write_text("GOODTOKEN")
    _patch_register(monkeypatch, cfg=_cfg(allow_self_reg=True))
    session = _FakeSession(execute_rows=[_user()])  # a user already exists
    _override_session(app_fixture, session)
    c = TestClient(app_fixture)

    r = c.post(
        "/api/auth/register",
        json={"email": "second@example.com", "password": "longenough"},
        headers={"x-forwarded-for": "198.51.100.63"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Subsequent user is NOT an admin and gets zero memberships.
    assert body["is_instance_admin"] is False
    assert body["default_person_record_id"] is None
    assert session.commits == 1
