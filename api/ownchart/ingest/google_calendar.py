"""Google Calendar adapter (FU-CAL-GOOGLE-OAUTH).

Pure helpers for the OAuth handshake + read-only events listing
against Google's Calendar API v3. No DB calls — the route layer and
worker layer wrap these into ``CalendarSource`` / ``CalendarEvent``
upserts via the same redactor (``redact_event_for_storage``) used
for the iOS adapter.

Doctrine pin: every credential / token / event load from this
module goes through ``READ_ONLY_SCOPES``. The OAuth callback
verifies Google's actual ``scope`` response is a subset of this
allowlist before persisting; a callback that came back with a
write scope is rejected (400) and no credential row is written.

Wire shape (`GoogleEventWire`) maps Google's REST event payload
into the SAME field set as ``IOSEventKitEvent``, so the storage
redactor + projector are adapter-agnostic. The only adapter-specific
piece is the projection function ``google_event_to_wire`` which
normalizes Google's quirks:

  - ``start/end`` are either ``{"date": "YYYY-MM-DD"}`` (all-day) or
    ``{"dateTime": "...", "timeZone": "..."}`` (timed).
  - ``status: "cancelled"`` is the deletion signal — projects to
    ``tombstoned=True`` on the wire shape.
  - ``iCalUID`` is the cross-calendar fingerprint Google preserves
    across calendar copies of the same event.

Operator-secrets discipline:
  - Client id / client secret / redirect URI are read from
    ``Settings`` (``OWNCHART_GOOGLE_CALENDAR_*``). Never from
    config.yaml, never logged, never echoed back to the UI.
  - ``is_google_calendar_configured()`` is the only public predicate
    other code should query before starting an OAuth flow.

The synchronous OAuth/event fetch primitives below are async so a
worker can run them inside the existing arq runtime; they take
``httpx.AsyncClient`` injection so tests don't need a network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..core.config import get_settings


# ---------------------------------------------------------------------------
# Doctrine constants


# Read-only scope set. Calendar Events read + calendars listing for
# the picker. NEVER add ``calendar.events`` (write) or ``calendar``
# (read+write) without an explicit doctrine update — this slice is
# read-only by design.
READ_ONLY_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
)

# Google's authorize + token + API base.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


# ---------------------------------------------------------------------------
# Public predicate (no Settings re-read at call sites)


def is_google_calendar_configured() -> bool:
    """True iff the operator has set all three Google Calendar env
    vars on this OwnChart deployment. Route layer uses this to
    decide between starting the OAuth flow and returning 503."""
    s = get_settings()
    return bool(
        s.google_calendar_client_id
        and s.google_calendar_client_secret
        and s.google_calendar_redirect_uri
    )


# ---------------------------------------------------------------------------
# Authorize URL


def build_authorize_url(
    *,
    state: str,
    extra_scopes: tuple[str, ...] = (),
) -> str:
    """Compose the Google OAuth2 authorize URL.

    ``state`` is the signed payload from ``core.oauth_state``. We
    pass ``access_type=offline`` + ``prompt=consent`` so Google
    always returns a refresh token, not just an access token (the
    default is online which doesn't grant refresh).

    ``extra_scopes`` is reserved for future widening; the default
    list is the read-only set. Callers should NOT pass write scopes
    — the callback will reject them — but we don't filter here so a
    test can exercise the rejection path.

    Query string is built via ``httpx.QueryParams.__str__`` so
    every value is percent-encoded (spaces in ``scope``, slashes
    in ``redirect_uri``, etc.). An earlier version hand-joined
    decoded values which produced raw spaces and unencoded paths.
    """
    if not is_google_calendar_configured():
        raise RuntimeError(
            "google_calendar_not_configured: operator must set "
            "OWNCHART_GOOGLE_CALENDAR_CLIENT_ID/_SECRET/_REDIRECT_URI"
        )
    s = get_settings()
    scopes = tuple(READ_ONLY_SCOPES) + tuple(extra_scopes)
    params = {
        "client_id": s.google_calendar_client_id.get_secret_value(),  # type: ignore[union-attr]
        "redirect_uri": s.google_calendar_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",
        # prompt=consent guarantees a refresh_token even if the user
        # has previously consented (otherwise Google can omit the
        # refresh_token on re-grant, breaking the worker).
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    qs = str(httpx.QueryParams(params))
    return f"{GOOGLE_AUTHORIZE_URL}?{qs}"


# ---------------------------------------------------------------------------
# Token exchange + refresh


class GoogleAuthError(RuntimeError):
    """Raised when Google rejects a token request. The route layer
    catches and maps to 400 (bad code) or 401 (revoked refresh)."""


async def exchange_code_for_token(
    *,
    code: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Trade the OAuth ``code`` for an access + refresh token.

    Returns the raw Google response dict. Caller is responsible for
    encrypting + persisting ``refresh_token`` and ``access_token``.

    Raises ``GoogleAuthError`` on non-2xx or missing refresh_token.
    The latter is critical — without a refresh token, the worker
    can't sync past the first access token expiry.
    """
    if not is_google_calendar_configured():
        raise GoogleAuthError("google_calendar_not_configured")
    s = get_settings()
    body = {
        "code": code,
        "client_id": s.google_calendar_client_id.get_secret_value(),  # type: ignore[union-attr]
        "client_secret": s.google_calendar_client_secret.get_secret_value(),  # type: ignore[union-attr]
        "redirect_uri": s.google_calendar_redirect_uri,
        "grant_type": "authorization_code",
    }
    owned = client is None
    cli = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await cli.post(GOOGLE_TOKEN_URL, data=body)
        if r.status_code != 200:
            raise GoogleAuthError(
                f"google_token_exchange_failed: HTTP {r.status_code}"
            )
        out = r.json()
        if "refresh_token" not in out:
            # Google omits refresh_token on re-grants without
            # prompt=consent — build_authorize_url forces consent,
            # so this should not happen in normal operation.
            raise GoogleAuthError("google_response_missing_refresh_token")
        return out
    finally:
        if owned:
            await cli.aclose()


async def refresh_access_token(
    *,
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an expired access token. Returns the raw Google
    response (``access_token``, ``expires_in``, scope, …). The
    refresh_token itself is not rotated by Google on this flow.
    """
    if not is_google_calendar_configured():
        raise GoogleAuthError("google_calendar_not_configured")
    s = get_settings()
    body = {
        "refresh_token": refresh_token,
        "client_id": s.google_calendar_client_id.get_secret_value(),  # type: ignore[union-attr]
        "client_secret": s.google_calendar_client_secret.get_secret_value(),  # type: ignore[union-attr]
        "grant_type": "refresh_token",
    }
    owned = client is None
    cli = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await cli.post(GOOGLE_TOKEN_URL, data=body)
        if r.status_code == 401 or r.status_code == 400:
            # Google returns 400 with "invalid_grant" when the refresh
            # token has been revoked (user removed the app, password
            # change, etc.). Caller should mark the credential
            # revoked and require re-consent.
            raise GoogleAuthError("google_refresh_revoked")
        if r.status_code != 200:
            raise GoogleAuthError(
                f"google_refresh_failed: HTTP {r.status_code}"
            )
        return r.json()
    finally:
        if owned:
            await cli.aclose()


# ---------------------------------------------------------------------------
# Scope verification


def granted_scope_is_read_only(scope_response: str) -> bool:
    """Verify Google's response ``scope`` (space-separated) is a
    subset of the read-only allowlist.

    A user MAY grant extra scopes via the consent screen if they
    have other apps using the same OAuth client (rare in
    self-hosted), so we don't reject extras — we just refuse to
    persist a credential whose granted scope set includes WRITE
    scopes from Google Calendar.
    """
    granted = {s for s in (scope_response or "").split(" ") if s}
    # The forbidden set: any Google scope NOT ending in ``.readonly``
    # under the calendar namespace. Future widenings (e.g. profile
    # read) can land here.
    write_scope_prefix = "https://www.googleapis.com/auth/calendar"
    for g in granted:
        if not g.startswith(write_scope_prefix):
            # Non-calendar scopes (userinfo.email) are OK.
            continue
        if g.endswith(".readonly"):
            continue
        # Anything under calendar.* that ISN'T .readonly is a write
        # scope (calendar, calendar.events).
        return False
    return True


# ---------------------------------------------------------------------------
# Userinfo


async def fetch_userinfo(
    *,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the granting account's email + sub. We need the email
    to dedupe credential rows per Google account."""
    owned = client is None
    cli = client or httpx.AsyncClient(timeout=10.0)
    try:
        r = await cli.get(
            GOOGLE_USERINFO_URL,
            headers={"authorization": f"Bearer {access_token}"},
        )
        if r.status_code != 200:
            raise GoogleAuthError(
                f"google_userinfo_failed: HTTP {r.status_code}"
            )
        return r.json()
    finally:
        if owned:
            await cli.aclose()


# ---------------------------------------------------------------------------
# Calendar list


async def list_calendars(
    *,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """List the calendars the granting user can read. Used by the
    bind UI so the operator picks which Google calendars OwnChart
    should sync — never auto-bind everything."""
    owned = client is None
    cli = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await cli.get(
            f"{GOOGLE_CALENDAR_API}/users/me/calendarList",
            headers={"authorization": f"Bearer {access_token}"},
        )
        if r.status_code != 200:
            raise GoogleAuthError(
                f"google_calendar_list_failed: HTTP {r.status_code}"
            )
        return r.json().get("items", [])
    finally:
        if owned:
            await cli.aclose()


# ---------------------------------------------------------------------------
# Event fetch + wire-shape projection


async def list_events(
    *,
    access_token: str,
    calendar_external_id: str,
    time_min: datetime,
    time_max: datetime,
    sync_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List events in the window. Returns (events, next_sync_token).

    Google supports incremental sync via ``syncToken``: pass the
    token from a prior response to fetch only changes since that
    point. The worker stores the token in ``raw_metadata.calendar``
    on the CalendarSource (out-of-band of the per-event metadata).

    ``time_min`` + ``time_max`` are RFC3339; we coerce to UTC ISO
    with ``Z`` suffix because Google rejects ``+00:00`` in some
    edge versions.
    """
    owned = client is None
    cli = client or httpx.AsyncClient(timeout=30.0)
    try:
        params: dict[str, str] = {
            "maxResults": "2500",
            "singleEvents": "true",  # expand recurrences inline
            "showDeleted": "true",   # so we can tombstone
            "orderBy": "startTime",
        }
        if sync_token:
            # syncToken can't combine with time bounds per Google's API.
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = _to_rfc3339(time_min)
            params["timeMax"] = _to_rfc3339(time_max)

        # Percent-encode the calendar id as a path segment. Google
        # calendar IDs commonly contain reserved characters
        # (``#`` for group calendars like ``family#events@group``,
        # ``@`` for the address-form id). Without encoding, ``#``
        # truncates the URL at the fragment boundary and the
        # request reaches ``/calendars/family`` — wrong calendar.
        # safe="" forces ``@``, ``+``, ``/``, ``:`` to be encoded too.
        calendar_id = quote(calendar_external_id, safe="")
        all_events: list[dict[str, Any]] = []
        next_sync_token: str | None = None
        next_page_token: str | None = None
        while True:
            if next_page_token:
                params["pageToken"] = next_page_token
            r = await cli.get(
                f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events",
                params=params,
                headers={"authorization": f"Bearer {access_token}"},
            )
            if r.status_code != 200:
                raise GoogleAuthError(
                    f"google_events_list_failed: HTTP {r.status_code}"
                )
            body = r.json()
            all_events.extend(body.get("items") or [])
            next_page_token = body.get("nextPageToken")
            if next_page_token:
                continue
            next_sync_token = body.get("nextSyncToken")
            break
        return all_events, next_sync_token
    finally:
        if owned:
            await cli.aclose()


def _to_rfc3339(dt: datetime) -> str:
    """Coerce a tz-aware datetime to RFC3339 with ``Z`` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def google_event_to_wire(ev: dict[str, Any]) -> dict[str, Any]:
    """Project a Google Calendar Events resource into the same wire
    shape as ``IOSEventKitEvent`` so the storage redactor / projector
    work uniformly across adapters.

    Returns the dict form (not the Pydantic model) so the caller can
    feed it to ``IOSEventKitEvent.model_validate(...)`` if it wants
    Pydantic validation, OR call ``redact_event_for_storage()``
    directly on the dict.
    """
    status = ev.get("status")
    tombstoned = status == "cancelled"
    start_raw = ev.get("start") or {}
    end_raw = ev.get("end") or {}
    all_day = "date" in start_raw and "dateTime" not in start_raw

    if all_day:
        start_at = _parse_date_or_datetime(start_raw.get("date"))
        end_at = _parse_date_or_datetime(end_raw.get("date"))
    else:
        start_at = _parse_date_or_datetime(start_raw.get("dateTime"))
        end_at = _parse_date_or_datetime(end_raw.get("dateTime"))

    modified = ev.get("updated") or ev.get("created")
    external_modified_at = (
        _parse_date_or_datetime(modified) if modified else start_at
    )

    return {
        # Google's ``id`` is stable per (calendar, event) — fine as
        # our per-source external_id.
        "external_id": ev.get("id"),
        "external_modified_at": external_modified_at,
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
        "title": ev.get("summary"),
        "location": ev.get("location"),
        "notes": ev.get("description"),
        "attendees_count": (
            len(ev.get("attendees", [])) if ev.get("attendees") else None
        ),
        "metadata": {
            "recurrence": ev.get("recurringEventId"),
            "google_html_link": ev.get("htmlLink"),
            "google_event_type": ev.get("eventType"),
        },
        "tombstoned": tombstoned,
        "ical_uid": ev.get("iCalUID"),
        # Google reports time_zone on the calendar, not per-event,
        # for timed events the dateTime carries it. The worker pulls
        # the calendar default tz separately when binding.
        "time_zone": (
            start_raw.get("timeZone") or end_raw.get("timeZone") or None
        ),
    }


def _parse_date_or_datetime(s: str | None) -> datetime | None:
    """Accept either ``YYYY-MM-DD`` (all-day) or RFC3339; return a
    UTC-naive-tagged datetime so the storage column accepts it."""
    if not s:
        return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    # RFC3339; Python's fromisoformat handles ``Z`` suffix in 3.11+.
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)
