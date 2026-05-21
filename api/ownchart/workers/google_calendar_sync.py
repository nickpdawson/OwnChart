"""Google Calendar incremental sync worker (FU-CAL-GOOGLE-OAUTH).

One arq task: ``sync_google_calendar_source(source_id)``. Refreshes
the OAuth access token if expired, lists events in the source's
configured ``history_window_back`` window, normalizes each Google
event into the wire shape, redacts via the existing
``redact_event_for_storage`` (defense in depth — Google's payload
goes through the same authoritative redactor that ios_eventkit
does), and upserts into ``calendar_events`` with the same
idempotency key used by the iOS adapter.

Mirrors the iOS adapter's contracts:

  - ``status='cancelled'`` from Google → ``tombstoned=True`` on the
    wire, which becomes a soft-delete via the route layer's
    ``tombstoned_at = now`` UPDATE.
  - Recurring events: ``singleEvents=true`` is passed to Google's
    list endpoint so recurrences are expanded inline. We treat each
    instance as a separate event row, same as the iOS adapter.
  - Privacy redaction: server reapplies privacy_mode regardless of
    what Google sent.
  - ``last_sync_at`` + ``last_sync_status`` stamped on the
    CalendarSource at end (FU-CAL-SOURCE-STATUS contract).

Token rotation lives entirely inside this worker — the route layer
never refreshes; it persists the initial pair from the OAuth
callback and trusts the worker to keep them current.

Failure modes:
  - Network / Google 5xx: leave ``last_sync_status='error'``, set
    ``last_error`` on the credential, raise so arq retries.
  - 401 invalid_grant (refresh revoked): credential row flips to
    ``status='revoked'``, requires re-consent. Worker does NOT
    retry.
  - Read-only scope drift (Google somehow returns a write scope on
    refresh — defensive): credential flips to ``status='error'``
    with a descriptive message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.crypto import decrypt_str, encrypt
from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..ingest.calendar_eventkit import (
    IOSEventKitEvent,
    redact_event_for_storage,
)
from ..ingest.google_calendar import (
    GoogleAuthError,
    granted_scope_is_read_only,
    google_event_to_wire,
    list_events,
    refresh_access_token,
)
from ..models.calendar_event import CalendarEvent
from ..models.calendar_oauth_credential import CalendarOAuthCredential
from ..models.calendar_source import CalendarSource

log = get_logger("ownchart.workers.google_calendar_sync")


# Map history_window_back to a back-window timedelta.
_HISTORY_DELTAS: dict[str, timedelta | None] = {
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
    "3y": timedelta(days=365 * 3),
    "5y": timedelta(days=365 * 5),
    "all": None,  # caller treats None as "go far back enough"
}
_FORWARD_WINDOW = timedelta(days=365)  # mirror iOS 12-month forward


async def sync_google_calendar_source(
    ctx: dict[str, Any], source_id: str,
) -> dict[str, Any]:
    """Arq entry point. ``source_id`` is the CalendarSource UUID."""
    src_uuid = uuid.UUID(source_id)

    async with SessionLocal() as db:
        src = await db.get(CalendarSource, src_uuid)
        if src is None:
            log.warning("google_sync_source_missing", source_id=source_id)
            return {"error": "source_missing"}
        if src.adapter_type != "google_calendar":
            log.warning(
                "google_sync_wrong_adapter",
                source_id=source_id, adapter=src.adapter_type,
            )
            return {"error": "wrong_adapter"}
        if src.oauth_credential_id is None:
            log.warning("google_sync_credential_missing", source_id=source_id)
            return {"error": "credential_missing"}
        cred = await db.get(
            CalendarOAuthCredential, src.oauth_credential_id,
        )
        if cred is None or cred.status != "connected":
            log.warning(
                "google_sync_credential_not_connected",
                source_id=source_id,
                credential_status=cred.status if cred else None,
            )
            return {"error": "credential_not_connected"}

        # Capture record_id + decrypted refresh token while attached;
        # we exit the session before the Google calls.
        record_id = src.person_record_id
        privacy_mode = src.privacy_mode
        history_window_back = src.history_window_back
        external_id = src.external_id
        refresh_token = decrypt_str(cred.refresh_token_enc)
        access_token = decrypt_str(cred.access_token_enc)
        access_expires_at = cred.access_token_expires_at

    if not refresh_token:
        log.warning(
            "google_sync_refresh_token_undecryptable", source_id=source_id,
        )
        return {"error": "refresh_token_undecryptable"}

    # Refresh access token if missing or close to expiry.
    needs_refresh = (
        not access_token
        or access_expires_at is None
        or access_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60)
    )
    if needs_refresh:
        try:
            tok = await refresh_access_token(refresh_token=refresh_token)
        except GoogleAuthError as e:
            new_status = "revoked" if "revoked" in str(e) else "error"
            async with SessionLocal() as db:
                await db.execute(
                    update(CalendarOAuthCredential)
                    .where(CalendarOAuthCredential.id == cred.id)
                    .values(
                        status=new_status,
                        last_error=str(e)[:2000],
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
            log.warning(
                "google_sync_refresh_failed",
                source_id=source_id, error=str(e), new_status=new_status,
            )
            return {"error": str(e)}
        # Defensive: re-check the refresh response scope.
        if not granted_scope_is_read_only(tok.get("scope", "")):
            async with SessionLocal() as db:
                await db.execute(
                    update(CalendarOAuthCredential)
                    .where(CalendarOAuthCredential.id == cred.id)
                    .values(
                        status="error",
                        last_error="refresh_returned_write_scope",
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
            return {"error": "refresh_returned_write_scope"}
        access_token = tok["access_token"]
        new_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(tok.get("expires_in") or 3600),
        )
        async with SessionLocal() as db:
            await db.execute(
                update(CalendarOAuthCredential)
                .where(CalendarOAuthCredential.id == cred.id)
                .values(
                    access_token_enc=encrypt(access_token),
                    access_token_expires_at=new_expires_at,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

    # Compute the time window.
    now = datetime.now(timezone.utc)
    back = _HISTORY_DELTAS.get(history_window_back) or timedelta(days=365 * 10)
    time_min = now - back
    time_max = now + _FORWARD_WINDOW

    try:
        events, _next_sync_token = await list_events(
            access_token=access_token,
            calendar_external_id=external_id,
            time_min=time_min,
            time_max=time_max,
        )
    except GoogleAuthError as e:
        async with SessionLocal() as db:
            await db.execute(
                update(CalendarOAuthCredential)
                .where(CalendarOAuthCredential.id == cred.id)
                .values(
                    last_error=str(e)[:2000],
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        log.warning(
            "google_sync_list_events_failed",
            source_id=source_id, error=str(e),
        )
        return {"error": str(e)}

    # Upsert via the same redactor the iOS path uses.
    accepted = 0
    tombstoned_count = 0
    upsert_now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        for raw_ev in events:
            wire = google_event_to_wire(raw_ev)
            if not wire.get("external_id") or not wire.get("start_at"):
                continue  # skip malformed
            # Validate via Pydantic; same wire model the iOS adapter uses.
            try:
                model = IOSEventKitEvent.model_validate(wire)
            except Exception:  # noqa: BLE001
                continue  # skip events that fail validation
            redacted = redact_event_for_storage(
                model.model_dump(),
                privacy_mode=privacy_mode,
                sync_mode="incremental",
            )
            if redacted["tombstoned"]:
                await db.execute(
                    update(CalendarEvent)
                    .where(CalendarEvent.calendar_source_id == src_uuid)
                    .where(CalendarEvent.external_id == redacted["external_id"])
                    .where(CalendarEvent.tombstoned_at.is_(None))
                    .values(tombstoned_at=upsert_now, updated_at=upsert_now)
                )
                tombstoned_count += 1
                continue
            await db.execute(
                pg_insert(CalendarEvent.__table__)
                .values(
                    id=uuid.uuid4(),
                    person_record_id=record_id,
                    calendar_source_id=src_uuid,
                    external_id=redacted["external_id"],
                    external_modified_at=redacted["external_modified_at"],
                    start_at=redacted["start_at"],
                    end_at=redacted["end_at"],
                    all_day=redacted["all_day"],
                    title=redacted["title"],
                    location=redacted["location"],
                    notes=redacted["notes"],
                    attendees_count=redacted["attendees_count"],
                    privacy_mode_applied=redacted["privacy_mode_applied"],
                    raw_metadata=redacted["raw_metadata"],
                    tombstoned_at=None,
                )
                .on_conflict_do_update(
                    constraint="calendar_events_source_external_uq",
                    set_={
                        "external_modified_at": redacted["external_modified_at"],
                        "start_at": redacted["start_at"],
                        "end_at": redacted["end_at"],
                        "all_day": redacted["all_day"],
                        "title": redacted["title"],
                        "location": redacted["location"],
                        "notes": redacted["notes"],
                        "attendees_count": redacted["attendees_count"],
                        "privacy_mode_applied": redacted["privacy_mode_applied"],
                        "raw_metadata": redacted["raw_metadata"],
                        "tombstoned_at": None,
                        "updated_at": upsert_now,
                    },
                )
            )
            accepted += 1

        # Stamp sync health on the source (FU-CAL-SOURCE-STATUS).
        await db.execute(
            update(CalendarSource)
            .where(CalendarSource.id == src_uuid)
            .values(
                last_sync_at=upsert_now,
                last_sync_status=(
                    "ok" if (accepted + tombstoned_count) > 0 else "empty"
                ),
                updated_at=upsert_now,
            )
        )
        # And on the credential row so the UI can surface "last
        # successful Google sync" even before binding more calendars.
        await db.execute(
            update(CalendarOAuthCredential)
            .where(CalendarOAuthCredential.id == cred.id)
            .values(
                last_synced_at=upsert_now,
                last_error=None,
                updated_at=upsert_now,
            )
        )
        await db.commit()

    log.info(
        "google_sync_completed",
        source_id=source_id,
        accepted=accepted,
        tombstoned=tombstoned_count,
        privacy_mode=privacy_mode,
        history_window_back=history_window_back,
    )
    return {"accepted": accepted, "tombstoned": tombstoned_count}
