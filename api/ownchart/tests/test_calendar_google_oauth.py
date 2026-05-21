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
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
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
    list_events,
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
# 6. Authorize URL composition — pins critical params via parse_qs so
#    encoding bugs (raw spaces in scope, unencoded redirect_uri /
#    state, etc.) surface as round-trip mismatches.


def _parse_authorize_url(url: str) -> dict[str, list[str]]:
    parts = urlsplit(url)
    return parse_qs(parts.query, keep_blank_values=True)


def test_authorize_url_query_round_trips_via_parse_qs(monkeypatch):
    """Parse the URL with urlsplit+parse_qs and assert every critical
    param round-trips to the exact intended value. This catches
    encoding regressions (raw spaces, missing %2F on redirect_uri,
    truncated state) that ``in url`` substring checks miss."""
    _configured_settings(monkeypatch)
    state = "signed.state.value-with/slashes+plus=eq"
    url = build_authorize_url(state=state)

    # No literal ASCII space in the URL.
    assert " " not in url, f"raw space leaked into URL: {url!r}"

    parts = urlsplit(url)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == GOOGLE_AUTHORIZE_URL
    )
    q = parse_qs(parts.query, keep_blank_values=True)

    # client_id round-trips exactly (not the secret — that's a
    # client-side public id).
    assert q["client_id"] == ["test-client-id"]

    # state round-trips exactly, slashes/plus/equals and all.
    assert q["state"] == [state]

    # redirect_uri round-trips to the configured value.
    assert q["redirect_uri"] == [
        "https://example.test/api/calendar/google/callback",
    ]

    # response_type + offline + consent invariants.
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["include_granted_scopes"] == ["true"]

    # scope round-trips to the read-only set (space-separated,
    # but the URL encoding is invisible to parse_qs).
    scope_values = q["scope"][0].split(" ")
    assert set(scope_values) == set(READ_ONLY_SCOPES)


def test_authorize_url_scope_decodes_to_read_only_scopes(monkeypatch):
    """Specifically pin that the decoded ``scope`` value is exactly
    the read-only allowlist, in stable order. A future widening
    that accidentally adds a write scope shows up here first."""
    _configured_settings(monkeypatch)
    url = build_authorize_url(state="X")
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    decoded_scopes = q["scope"][0].split(" ")
    for s in decoded_scopes:
        assert s in READ_ONLY_SCOPES, f"unexpected scope leaked: {s}"
    for s in READ_ONLY_SCOPES:
        assert s in decoded_scopes, f"missing read-only scope: {s}"


def test_authorize_url_does_not_request_write_scopes(monkeypatch):
    """A parse_qs round-trip check that no Calendar write scope
    appears in the decoded scope list."""
    _configured_settings(monkeypatch)
    url = build_authorize_url(state="X")
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    decoded_scopes = q["scope"][0].split(" ")
    write_scopes = {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    }
    for ws in write_scopes:
        assert ws not in decoded_scopes


def test_authorize_url_redirect_uri_is_percent_encoded(monkeypatch):
    """The redirect_uri value contains ``/`` and ``:`` which MUST
    be percent-encoded in the query string. parse_qs decodes for
    us; we additionally check the raw query for the encoded form
    so a regression that pastes the URL verbatim trips."""
    _configured_settings(monkeypatch)
    url = build_authorize_url(state="X")
    parts = urlsplit(url)
    # The raw query must contain the percent-encoded scheme separator.
    assert "redirect_uri=https%3A%2F%2F" in parts.query, (
        f"redirect_uri not percent-encoded: query={parts.query!r}"
    )


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


# ---------------------------------------------------------------------------
# 9. list_events percent-encodes the calendar id as a path segment.
#
# Google calendar IDs commonly contain ``#`` (group calendars like
# ``family#events@group.calendar.google.com``) and ``@`` (the
# canonical address form). Without explicit percent-encoding, ``#``
# truncates the URL at the fragment boundary on the wire — the
# request reaches ``/calendars/family`` and Google 404s. Pin the
# encoding here so a refactor that switches back to raw interpolation
# trips immediately.


@pytest.mark.asyncio
async def test_list_events_percent_encodes_calendar_id_with_hash_and_at():
    """A calendar id containing ``#`` and ``@`` must end up
    percent-encoded in the requested URL — both characters are
    reserved as path-segment delimiters / fragment markers."""

    captured: dict[str, str] = {}

    class _FakeResponse:
        status_code = 200
        def json(self) -> dict:
            return {"items": [], "nextSyncToken": "tok"}

    class _FakeClient:
        async def get(self, url: str, **kwargs):
            captured["url"] = url
            captured["params"] = dict(kwargs.get("params") or {})
            return _FakeResponse()

    cal_id = "family#events@example.com"
    await list_events(
        access_token="fake-access",
        calendar_external_id=cal_id,
        time_min=datetime(2026, 1, 1, tzinfo=timezone.utc),
        time_max=datetime(2026, 2, 1, tzinfo=timezone.utc),
        client=_FakeClient(),
    )

    url = captured["url"]
    # Raw ``#`` must NOT survive into the URL path.
    assert "#" not in url, (
        f"raw '#' leaked into URL — fragment would truncate: {url!r}"
    )
    # Raw ``@`` must not survive either when safe="" is used. (The
    # underlying httpx client may pass it through to httpcore, but
    # the URL WE construct should already be encoded.)
    path_only = url.split("?", 1)[0]
    assert "@" not in path_only, (
        f"raw '@' leaked into URL path: {path_only!r}"
    )
    # Percent-encoded forms must be present.
    assert "%23" in url, f"missing %23 (encoded #): {url!r}"
    assert "%40" in path_only, f"missing %40 (encoded @): {path_only!r}"
    # The URL must still target the events sub-resource on the
    # calendars/{id} collection — not a fragment-truncated form.
    assert "/calendars/" in url
    assert url.endswith("/events") or "/events?" in url


@pytest.mark.asyncio
async def test_list_events_simple_calendar_id_round_trips():
    """A simple calendar id (no reserved chars) survives encoding
    too — sanity check that ``quote(..., safe="")`` doesn't mangle
    plain ASCII slugs."""
    captured: dict[str, str] = {}

    class _FakeResponse:
        status_code = 200
        def json(self) -> dict:
            return {"items": [], "nextSyncToken": "tok"}

    class _FakeClient:
        async def get(self, url: str, **kwargs):
            captured["url"] = url
            return _FakeResponse()

    await list_events(
        access_token="fake-access",
        calendar_external_id="primary",
        time_min=datetime(2026, 1, 1, tzinfo=timezone.utc),
        time_max=datetime(2026, 2, 1, tzinfo=timezone.utc),
        client=_FakeClient(),
    )
    assert "/calendars/primary/events" in captured["url"]


def test_list_events_source_uses_quote_with_safe_empty():
    """Static-source pin: list_events must call quote() with
    safe="" so reserved characters (``@``, ``+``, ``/``, ``:``)
    are also encoded. A default safe="/" would leave ``/`` raw
    and break group-calendar IDs."""
    from ownchart.ingest import google_calendar as gc
    src = inspect.getsource(gc.list_events)
    assert "quote(calendar_external_id, safe=" in src
    # The ``safe=""`` form (empty string) is the load-bearing
    # detail; assert it specifically.
    assert 'quote(calendar_external_id, safe="")' in src
