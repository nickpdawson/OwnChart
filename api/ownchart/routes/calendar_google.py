"""Google Calendar OAuth routes (FU-CAL-GOOGLE-OAUTH).

Three endpoints, one outbound flow:

  ``POST /api/calendar/google/connect-start`` (caregiver+)
      Returns a Google authorize URL + signed state. Returns 503
      when the operator hasn't configured Google Calendar env vars
      — never silently fails open or surfaces raw client_id /
      client_secret fields.

  ``GET  /api/calendar/google/callback?code=&state=`` (caregiver+)
      Validates the signed state, exchanges the auth code for
      tokens, verifies the granted scope is a subset of the
      read-only allowlist, fetches the granting account email,
      stores an encrypted ``CalendarOAuthCredential`` bound to the
      SIGNED person_record (not the active record), lists the
      account's calendars, and returns the picker payload. The
      callback NEVER auto-creates CalendarSource rows — the user
      explicitly picks which calendars to bind via the next route.

  ``POST /api/calendar/google/credentials/{credential_id}/bind``
      (caregiver+)
      Body: ``{external_id, display_name, privacy_mode,
      llm_full_details_consent, history_window_back}``. Creates a
      ``CalendarSource`` with adapter_type='google_calendar' and
      enqueues the first sync. Idempotent on
      ``(user, record, adapter, external_id)``: re-binding the
      same Google calendar reuses the existing row.

Doctrine pins enforced at the route layer:
  - Operator secrets discipline: no client_id/client_secret
    rendered or echoed; 503 ("not configured by this OwnChart
    operator") on missing config.
  - Cross-record callback: the credential row binds to the SIGNED
    person_record_id from the state, not to ``ctx.active_record_id``.
    A user that switched tabs mid-flow lands their Google account
    on the originating record.
  - Read-only scope enforcement: callback rejects (400) and writes
    no credential row when ``granted_scope_is_read_only`` returns
    False.
  - Encrypted-at-rest tokens: refresh + access tokens go through
    ``core.crypto.encrypt`` before they touch the DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.arq_pool import enqueue_google_calendar_sync
from ..core.auth_context import AuthContext, require_role
from ..core.crypto import encrypt
from ..core.db import get_session
from ..core.logger import get_logger
from ..core.oauth_state import (
    OAuthStateError,
    decode_oauth_state,
    sign_oauth_state,
)
from ..ingest.calendar_eventkit import PrivacyMode
from ..ingest.google_calendar import (
    GoogleAuthError,
    build_authorize_url,
    exchange_code_for_token,
    fetch_userinfo,
    google_event_to_wire,  # re-exported for tests; not used in this module
    granted_scope_is_read_only,
    is_google_calendar_configured,
    list_calendars,
)
from ..models.calendar_oauth_credential import CalendarOAuthCredential
from ..models.calendar_source import CalendarSource

router = APIRouter()
log = get_logger("ownchart.routes.calendar_google")


# ---------------------------------------------------------------------------
# IO shapes


class GoogleConnectStartResponse(BaseModel):
    authorize_url: str
    state: str


class GoogleCalendarChoice(BaseModel):
    external_id: str
    summary: str
    primary: bool = False
    time_zone: str | None = None
    background_color: str | None = None


class GoogleCallbackResponse(BaseModel):
    credential_id: str
    google_account_email: str
    calendars: list[GoogleCalendarChoice]


class GoogleBindRequest(BaseModel):
    external_id: str = Field(..., max_length=256)
    display_name: str = Field(..., max_length=256)
    privacy_mode: PrivacyMode = "title_and_time"
    llm_full_details_consent: bool = False
    history_window_back: str = "90d"


class GoogleBindResponse(BaseModel):
    source_id: str
    adapter_type: str
    external_id: str
    display_name: str
    privacy_mode: str
    history_window_back: str


# ---------------------------------------------------------------------------
# 503 helper — keep error shape identical across the three endpoints.


def _503_not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Google Calendar is not configured by this OwnChart "
            "operator. Ask your administrator to set "
            "OWNCHART_GOOGLE_CALENDAR_CLIENT_ID, _SECRET, and "
            "_REDIRECT_URI."
        ),
    )


# ---------------------------------------------------------------------------
# 1. Connect start


@router.post("/connect-start", response_model=GoogleConnectStartResponse)
async def connect_start(
    ctx: AuthContext = Depends(require_role("caregiver")),
) -> GoogleConnectStartResponse:
    """Return a Google OAuth authorize URL + the signed state token
    the callback will re-verify."""
    if not is_google_calendar_configured():
        raise _503_not_configured()
    state = sign_oauth_state(
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
    )
    url = build_authorize_url(state=state)
    log.info(
        "calendar_google_oauth_start",
        user_id=str(ctx.user.id),
        person_record_id=str(ctx.active_record_id),
    )
    return GoogleConnectStartResponse(authorize_url=url, state=state)


# ---------------------------------------------------------------------------
# 2. Callback


@router.get("/callback", response_model=GoogleCallbackResponse)
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> GoogleCallbackResponse:
    """Handle Google's redirect: validate state, exchange code,
    persist an encrypted credential row, return the calendar
    picker."""
    if not is_google_calendar_configured():
        raise _503_not_configured()

    try:
        payload = decode_oauth_state(state)
    except OAuthStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"oauth_state_invalid: {e}",
        ) from None

    # The signed state's user MUST match the authenticated session.
    # If a different user clicks the Google redirect, reject — never
    # bind tokens to a session that didn't initiate the flow.
    if payload.user_id != ctx.user.id:
        log.warning(
            "calendar_google_callback_user_mismatch",
            state_user=str(payload.user_id),
            session_user=str(ctx.user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="oauth_state_user_mismatch",
        )

    # Exchange the code for tokens.
    try:
        tok = await exchange_code_for_token(code=code)
    except GoogleAuthError as e:
        # Map Google failures to client-friendly errors. The route
        # never logs the code itself; only the failure class.
        log.warning("calendar_google_token_exchange_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"google_token_exchange_failed: {e}",
        ) from None

    # Read-only enforcement: refuse to persist a credential whose
    # granted scope set contains a Calendar write scope.
    scope_response = tok.get("scope", "")
    if not granted_scope_is_read_only(scope_response):
        log.warning(
            "calendar_google_callback_write_scope_rejected",
            scope=scope_response,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "google_scope_not_read_only: this OwnChart deployment "
                "only accepts read-only Calendar scopes."
            ),
        )

    access_token = tok["access_token"]
    refresh_token = tok["refresh_token"]
    expires_in = int(tok.get("expires_in") or 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Fetch the granting account email so we can dedupe by account.
    try:
        userinfo = await fetch_userinfo(access_token=access_token)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"google_userinfo_failed: {e}",
        ) from None
    email = userinfo.get("email") or ""
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="google_userinfo_missing_email",
        )

    # Upsert the credential row. Idempotent on the UNIQUE key so
    # re-consent under the same Google account refreshes the row.
    enc_refresh = encrypt(refresh_token)
    enc_access = encrypt(access_token)
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(CalendarOAuthCredential.__table__)
        .values(
            id=uuid.uuid4(),
            user_id=ctx.user.id,
            person_record_id=payload.person_record_id,
            provider="google",
            google_account_email=email,
            refresh_token_enc=enc_refresh,
            access_token_enc=enc_access,
            access_token_expires_at=expires_at,
            scope_granted=scope_response,
            status="connected",
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="calendar_oauth_credentials_uq",
            set_={
                "refresh_token_enc": enc_refresh,
                "access_token_enc": enc_access,
                "access_token_expires_at": expires_at,
                "scope_granted": scope_response,
                "status": "connected",
                "last_error": None,
                "updated_at": now,
            },
        )
        .returning(CalendarOAuthCredential.__table__)
    )
    cred_row = (await db.execute(stmt)).mappings().one()
    await db.commit()

    # Calendar picker payload.
    try:
        cals = await list_calendars(access_token=access_token)
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"google_calendar_list_failed: {e}",
        ) from None
    choices = [
        GoogleCalendarChoice(
            external_id=str(c.get("id") or ""),
            summary=str(c.get("summaryOverride") or c.get("summary") or ""),
            primary=bool(c.get("primary")),
            time_zone=c.get("timeZone"),
            background_color=c.get("backgroundColor"),
        )
        for c in cals if c.get("id")
    ]

    log.info(
        "calendar_google_oauth_connected",
        credential_id=str(cred_row["id"]),
        person_record_id=str(payload.person_record_id),
        calendars_returned=len(choices),
    )
    return GoogleCallbackResponse(
        credential_id=str(cred_row["id"]),
        google_account_email=email,
        calendars=choices,
    )


# ---------------------------------------------------------------------------
# 3. Bind a Google calendar to a CalendarSource


@router.post(
    "/credentials/{credential_id}/bind",
    response_model=GoogleBindResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bind_calendar(
    credential_id: uuid.UUID,
    body: GoogleBindRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> GoogleBindResponse:
    """Bind a specific Google calendar to a CalendarSource. The
    credential must belong to the active record — cross-record
    probes return 404."""
    if body.history_window_back not in ("90d", "1y", "3y", "5y", "all"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="history_window_back must be one of 90d/1y/3y/5y/all",
        )

    cred = (await db.execute(
        select(CalendarOAuthCredential)
        .where(CalendarOAuthCredential.id == credential_id)
        .where(CalendarOAuthCredential.person_record_id == ctx.active_record_id)
        .where(CalendarOAuthCredential.status == "connected")
    )).scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(CalendarSource.__table__)
        .values(
            id=uuid.uuid4(),
            person_record_id=ctx.active_record_id,
            user_id=ctx.user.id,
            adapter_type="google_calendar",
            external_id=body.external_id,
            display_name=body.display_name,
            privacy_mode=body.privacy_mode,
            llm_full_details_consent=body.llm_full_details_consent,
            connected_at=now,
            disconnected_at=None,
            history_window_back=body.history_window_back,
            oauth_credential_id=cred.id,
        )
        .on_conflict_do_update(
            constraint="calendar_sources_user_record_adapter_external_uq",
            set_={
                "display_name": body.display_name,
                "privacy_mode": body.privacy_mode,
                "llm_full_details_consent": body.llm_full_details_consent,
                "connected_at": now,
                "disconnected_at": None,
                "history_window_back": body.history_window_back,
                "oauth_credential_id": cred.id,
                "updated_at": now,
            },
        )
        .returning(CalendarSource.__table__)
    )
    row = (await db.execute(stmt)).mappings().one()
    await db.commit()

    # Kick off the first sync. The worker will refresh the access
    # token if needed, fetch events in the configured window, and
    # upsert via the same redactor used by the iOS adapter.
    await enqueue_google_calendar_sync(str(row["id"]))

    log.info(
        "calendar_google_bound",
        source_id=str(row["id"]),
        credential_id=str(cred.id),
        person_record_id=str(ctx.active_record_id),
        privacy_mode=body.privacy_mode,
        history_window_back=body.history_window_back,
    )
    return GoogleBindResponse(
        source_id=str(row["id"]),
        adapter_type=row["adapter_type"],
        external_id=row["external_id"],
        display_name=row["display_name"],
        privacy_mode=row["privacy_mode"],
        history_window_back=row["history_window_back"],
    )
