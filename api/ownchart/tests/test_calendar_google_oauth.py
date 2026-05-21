"""Google Calendar OAuth adapter tests (FU-CAL-GOOGLE-OAUTH).

Seven pillars per PM directive:

  1. OAuth state binds to person_record_id (signed payload).
  2. Missing Google operator config → 503 on connect-start AND callback.
  3. Read-only scope enforcement — callback rejects write scopes.
  4. Encrypted token storage — refresh + access tokens go through
     ``core.crypto.encrypt`` before they reach the DB; tests pin
     the encrypt() invocation and confirm no plaintext leaks the
     log/response surface.
  5. Multi-source dedupe — covered by the existing slice 3 wire-
     shape + redactor tests since the storage path is shared. This
     file adds the google→wire mapping invariants.
  6. History-window projection — covered in
     test_calendar_history_window.py (separate file for the
     Ask-projector slice).
  7. Ask projector privacy floor — covered in
     test_calendar_slice3.py + extended in
     test_calendar_history_window.py.

All tests in this file are pure-function or static-source. The DB
+ TestClient layer follows the same "perimeter contract" pattern
used in test_calendar_slice3.py — denied_client confirms 403
propagation; no real Google network calls.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ownchart.core.config import Settings, get_settings
from ownchart.core.oauth_state import (
    decode_oauth_state,
    sign_oauth_state,
)
from ownchart.ingest.google_calendar import (
    GOOGLE_AUTHORIZE_URL,
    GOOGLE_CALENDAR_API,
    READ_ONLY_SCOPES,
    build_authorize_url,
    google_event_to_wire,
    granted_scope_is_read_only,
    is_google_calendar_configured,
)


# ---------------------------------------------------------------------------
# Helpers


def _configured_settings(monkeypatch) -> None:
    """Set the three Google env vars + a session secret + DEK so the
    Settings cache returns a fully-configured instance."""
    monkeypatch.setenv("OWNCHART_GOOGLE_CALENDAR_CLIENT_ID", "test-client-id")
    monkeypatch.setenv(
        "OWNCHART_GOOGLE_CALENDAR_CLIENT_SECRET", "test-client-secret",
    )
    monkeypatch.setenv(
        "OWNCHART_GOOGLE_CALENDAR_REDIRECT_URI",
        "https://example.test/api/calendar/google/callback",
    )
    monkeypatch.setenv("OWNCHART_SESSION_SECRET", "test-session-secret-32b")
    monkeypatch.setenv(
        "OWNCHART_TOKEN_DEK",
        # base64 of 32 zero bytes — deterministic test DEK
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    get_settings.cache_clear()


def _unconfigured_settings(monkeypatch) -> None:
    """Drop the Google env vars to simulate operator non-config."""
    monkeypatch.delenv("OWNCHART_GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("OWNCHART_GOOGLE_CALENDAR_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OWNCHART_GOOGLE_CALENDAR_REDIRECT_URI", raising=False)
    monkeypatch.setenv("OWNCHART_SESSION_SECRET", "test-session-secret-32b")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. OAuth state binds to person_record_id


def test_oauth_state_carries_person_record_id(monkeypatch):
    _configured_settings(monkeypatch)
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    token = sign_oauth_state(user_id=uid, person_record_id=rid)
    payload = decode_oauth_state(token)
    assert payload.user_id == uid
    assert payload.person_record_id == rid
    # Each sign call generates a fresh csrf_nonce.
    other = sign_oauth_state(user_id=uid, person_record_id=rid)
    assert token != other


def test_callback_binds_to_signed_record_not_active_record(monkeypatch):
    """Static-source check on routes/calendar_google.callback —
    the credential INSERT must pass payload.person_record_id, never
    ctx.active_record_id. A user who switches tabs mid-flow lands
    their Google account on the originating record."""
    _configured_settings(monkeypatch)
    from ownchart.routes.calendar_google import callback
    src = inspect.getsource(callback)
    # The values block of the pg_insert must reference the signed
    # payload, not ctx.
    insert_block = src.split("pg_insert(CalendarOAuthCredential.__table__)")[1]
    insert_block = insert_block.split("on_conflict_do_update")[0]
    assert "person_record_id=payload.person_record_id" in insert_block
    assert "person_record_id=ctx.active_record_id" not in insert_block


# ---------------------------------------------------------------------------
# 2. Missing Google config → 503


def test_is_google_calendar_configured_false_without_env(monkeypatch):
    _unconfigured_settings(monkeypatch)
    assert is_google_calendar_configured() is False


def test_is_google_calendar_configured_true_with_env(monkeypatch):
    _configured_settings(monkeypatch)
    assert is_google_calendar_configured() is True


def test_build_authorize_url_raises_when_unconfigured(monkeypatch):
    _unconfigured_settings(monkeypatch)
    with pytest.raises(RuntimeError, match="google_calendar_not_configured"):
        build_authorize_url(state="anystate")


def test_connect_start_returns_503_when_unconfigured(monkeypatch, app_fixture):
    """Operator hasn't set Google env vars → 503 with a clear message,
    never falls through to building a half-broken authorize URL."""
    _unconfigured_settings(monkeypatch)
    from ownchart.tests.conftest import authed_client
    c = authed_client(app_fixture, role="caregiver")
    r = c.post("/api/calendar/google/connect-start")
    assert r.status_code == 503
    assert "not configured by this OwnChart operator" in r.text


def test_callback_returns_503_when_unconfigured(monkeypatch, app_fixture):
    _unconfigured_settings(monkeypatch)
    from ownchart.tests.conftest import authed_client
    c = authed_client(app_fixture, role="caregiver")
    r = c.get(
        "/api/calendar/google/callback?code=ignored&state=ignored",
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# 3. Read-only scope enforcement


def test_read_only_scope_allowlist_includes_only_readonly_calendar():
    """The READ_ONLY_SCOPES tuple must only contain Google scopes
    that end in .readonly under the calendar namespace, plus
    non-calendar scopes (userinfo.email). Any future widening to a
    write scope must come with explicit doctrine review."""
    calendar_scopes = [
        s for s in READ_ONLY_SCOPES if "/auth/calendar" in s
    ]
    for s in calendar_scopes:
        assert s.endswith(".readonly"), (
            f"Scope {s} is not a read-only Calendar scope; this is a "
            "doctrine violation — Google Calendar adapter is read-only."
        )


def test_granted_scope_passes_read_only_subset():
    # All read-only — pass.
    assert granted_scope_is_read_only(
        "https://www.googleapis.com/auth/calendar.readonly "
        "https://www.googleapis.com/auth/userinfo.email"
    ) is True


def test_granted_scope_rejects_calendar_write():
    # The unrestricted ``calendar`` scope is read+write — must reject.
    assert granted_scope_is_read_only(
        "https://www.googleapis.com/auth/calendar"
    ) is False


def test_granted_scope_rejects_calendar_events_write():
    assert granted_scope_is_read_only(
        "https://www.googleapis.com/auth/calendar.events"
    ) is False


def test_callback_static_rejects_write_scope():
    """Static-source check that the callback emits a 400 with the
    ``google_scope_not_read_only`` detail when
    granted_scope_is_read_only returns False, and does NOT proceed
    to persist a credential row."""
    from ownchart.routes.calendar_google import callback
    src = inspect.getsource(callback)
    assert "granted_scope_is_read_only(scope_response)" in src
    assert "google_scope_not_read_only" in src
    # The rejection must come BEFORE the credential INSERT.
    reject_idx = src.find("google_scope_not_read_only")
    insert_idx = src.find("pg_insert(CalendarOAuthCredential.__table__)")
    assert 0 < reject_idx < insert_idx


# ---------------------------------------------------------------------------
# 4. Encrypted token storage


def test_callback_encrypts_refresh_and_access_tokens():
    """Static-source check that both refresh_token and access_token
    are passed through ``encrypt()`` before they reach the INSERT."""
    from ownchart.routes.calendar_google import callback
    src = inspect.getsource(callback)
    # Both tokens go through encrypt() — never raw strings.
    assert "encrypt(refresh_token)" in src
    assert "encrypt(access_token)" in src
    # And the encrypted bytes are what lands on the table.
    assert "refresh_token_enc=enc_refresh" in src
    assert "access_token_enc=enc_access" in src


def test_worker_decrypts_refresh_token_then_re_encrypts_new_access():
    """The sync worker reads refresh_token via decrypt_str, then
    when it refreshes the access token, re-encrypts before
    persisting. Never stores plaintext tokens."""
    from ownchart.workers.google_calendar_sync import (
        sync_google_calendar_source,
    )
    src = inspect.getsource(sync_google_calendar_source)
    assert "decrypt_str(cred.refresh_token_enc)" in src
    assert "encrypt(access_token)" in src
    # And the persisted column is the ciphertext, not the plaintext.
    assert "access_token_enc=encrypt(access_token)" in src


def test_oauth_credential_model_token_columns_are_largebinary():
    """The DB columns holding refresh + access tokens must be
    LargeBinary (bytea) — a String column would imply we're
    storing plaintext."""
    from sqlalchemy import LargeBinary
    from ownchart.models.calendar_oauth_credential import (
        CalendarOAuthCredential,
    )
    cols = CalendarOAuthCredential.__table__.c
    assert isinstance(cols.refresh_token_enc.type, LargeBinary)
    assert isinstance(cols.access_token_enc.type, LargeBinary)


# ---------------------------------------------------------------------------
# 5. Google → wire-shape projection (feeds the multi-source dedupe
#    path; the dedupe itself is already pinned in slice 3 tests).


def test_google_event_to_wire_timed_event():
    ev = {
        "id": "ev-1",
        "status": "confirmed",
        "summary": "Dr. Patel — physical",
        "location": "Bozeman Health",
        "description": "Bring labs",
        "start": {"dateTime": "2026-01-15T10:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-01-15T11:00:00Z", "timeZone": "UTC"},
        "iCalUID": "google-ical-uid-1",
        "updated": "2026-01-10T08:00:00Z",
        "attendees": [{"email": "a@b"}, {"email": "c@d"}],
    }
    wire = google_event_to_wire(ev)
    assert wire["external_id"] == "ev-1"
    assert wire["tombstoned"] is False
    assert wire["all_day"] is False
    assert wire["title"] == "Dr. Patel — physical"
    assert wire["ical_uid"] == "google-ical-uid-1"
    assert wire["attendees_count"] == 2
    assert wire["start_at"] == datetime(
        2026, 1, 15, 10, 0, tzinfo=timezone.utc,
    )


def test_google_event_to_wire_all_day_event():
    ev = {
        "id": "ev-allday",
        "status": "confirmed",
        "summary": "Vacation",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-11"},
    }
    wire = google_event_to_wire(ev)
    assert wire["all_day"] is True
    assert wire["start_at"].year == 2026
    assert wire["start_at"].month == 7
    assert wire["start_at"].day == 4


def test_google_cancelled_event_projects_to_tombstoned():
    ev = {
        "id": "ev-cancelled",
        "status": "cancelled",
        "start": {"dateTime": "2026-01-15T10:00:00Z"},
        "end": {"dateTime": "2026-01-15T11:00:00Z"},
    }
    wire = google_event_to_wire(ev)
    assert wire["tombstoned"] is True


# ---------------------------------------------------------------------------
# 6. Authorize URL composition — pins critical params


def test_authorize_url_includes_critical_params(monkeypatch):
    _configured_settings(monkeypatch)
    url = build_authorize_url(state="STATE123")
    assert url.startswith(GOOGLE_AUTHORIZE_URL + "?")
    assert "client_id=test-client-id" in url
    assert "state=STATE123" in url
    # Refresh-token guarantee.
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    # Read-only scope landed in the URL.
    assert "calendar.readonly" in url
    # And it's NOT requesting any write scope.
    assert "auth/calendar%20" not in url  # bare 'calendar' = read+write
    assert "auth/calendar.events%20" not in url  # bare 'events' = write


def test_authorize_url_uses_configured_redirect_uri(monkeypatch):
    _configured_settings(monkeypatch)
    url = build_authorize_url(state="X")
    # URL-encoded form of the test redirect URI.
    assert "redirect_uri=https" in url
    assert "example.test" in url
    assert "callback" in url


# ---------------------------------------------------------------------------
# 7. Perimeter — denied_client coverage on the three Google routes


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/api/calendar/google/connect-start", None),
        (
            "POST",
            "/api/calendar/google/credentials/"
            "00000000-0000-0000-0000-000000000000/bind",
            {"external_id": "primary", "display_name": "x"},
        ),
    ],
)
def test_google_routes_403_on_record_access_revoked(
    monkeypatch, app_fixture, method, path, body,
):
    """Every record-scoped Google route returns 403 when AuthContext
    raises record_access_revoked — Slice 1 perimeter contract."""
    _configured_settings(monkeypatch)
    from ownchart.tests.conftest import denied_client
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path, json=body)
    assert r.status_code == 403


def test_callback_route_403_on_record_access_revoked(
    monkeypatch, app_fixture,
):
    """The callback also requires caregiver+ membership on the
    active record — even though the binding uses the signed
    person_record_id, the route is gated to prevent a caller
    without record access from triggering token exchanges at all."""
    _configured_settings(monkeypatch)
    from ownchart.tests.conftest import denied_client
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get(
        "/api/calendar/google/callback?code=x&state=y",
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 8. Doctrine — never log raw tokens


def test_callback_does_not_log_raw_tokens():
    """The callback must never log the access_token or refresh_token
    values themselves. Static-source check: no log.info /
    log.warning argument references the raw token variable."""
    from ownchart.routes.calendar_google import callback
    src = inspect.getsource(callback)
    # Find every log.* line and confirm none contain the raw
    # variable name.
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("log.", "logger.")):
            continue
        assert "refresh_token" not in stripped, (
            f"Found refresh_token in log line: {stripped}"
        )
        assert "access_token" not in stripped, (
            f"Found access_token in log line: {stripped}"
        )


def test_worker_does_not_log_raw_tokens():
    from ownchart.workers.google_calendar_sync import (
        sync_google_calendar_source,
    )
    src = inspect.getsource(sync_google_calendar_source)
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("log.", "logger.")):
            continue
        assert "refresh_token" not in stripped
        assert "access_token" not in stripped


def test_google_calendar_api_endpoints_pinned():
    """Doctrine pin: the calendar API base must be the v3 endpoint.
    A refactor that points at a different version (or worse, an
    untrusted host) breaks the security model."""
    from ownchart.ingest import google_calendar as gc
    assert gc.GOOGLE_AUTHORIZE_URL.startswith("https://accounts.google.com/")
    assert gc.GOOGLE_TOKEN_URL.startswith("https://oauth2.googleapis.com/")
    assert gc.GOOGLE_CALENDAR_API == "https://www.googleapis.com/calendar/v3"
    assert GOOGLE_CALENDAR_API == gc.GOOGLE_CALENDAR_API
