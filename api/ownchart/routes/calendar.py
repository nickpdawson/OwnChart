"""Calendar ingest routes (M02 Slice 3).

iOS-only adapter path. Google / ICS / CalDAV are out of scope; the
``ADAPTER_TYPES`` registry reserves their names so they can land
without disturbing this layer.

Six endpoints, all record-scoped via Slice 1 perimeter (AuthContext +
require_role + person_record_id stamping + cross-record 404):

  POST   /api/calendar/sources          create / re-pick a calendar
  GET    /api/calendar/sources          list active sources
  PATCH  /api/calendar/sources/{id}     update privacy / consent / name
  DELETE /api/calendar/sources/{id}     disconnect (soft) + cascade tombstone events
  POST   /api/calendar/ingest           batch upsert (or tombstone) events
  GET    /api/calendar/events           time-window listing for UI

Two privacy contracts run through these handlers:

  - **STORAGE redaction.** ``redact_event_for_storage`` runs at every
    UPSERT regardless of what iOS sent. iOS is expected to apply
    privacy_mode client-side too, but the server is the authoritative
    redactor. A privacy_mode tightening on PATCH triggers a redaction
    sweep over existing events on that source.

  - **LLM exposure floor.** ``GET /api/calendar/events`` returns
    stored fields verbatim — the user is allowed to see their own
    stored data. Ask retrieval uses ``project_event_for_llm`` (in
    ``ingest/calendar_eventkit.py``) instead, which gates on the
    source's ``llm_full_details_consent`` flag and floors at
    busy-only-equivalent until the user explicitly elevates.

The 30-day hard-delete worker (PM B-3) lives in
``ingest/calendar_eventkit.py::purge_tombstoned_calendar_events``;
scheduling is a separate wiring concern handled outside this slice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, require_role
from ..core.db import get_session
from ..core.logger import get_logger
from ..ingest.calendar_eventkit import (
    IOSEventKitEvent,
    PrivacyMode,
    redact_event_for_storage,
)
from ..models.calendar_event import CalendarEvent
from ..models.calendar_source import CalendarSource

router = APIRouter()
log = get_logger("ownchart.routes.calendar")


# Strictness rank — higher = looser. Used to detect a privacy_mode
# tightening on PATCH so we can sweep existing events.
_PRIVACY_RANK: dict[str, int] = {
    "busy_only": 0,
    "title_and_time": 1,
    "full_details": 2,
}


# ---------------------------------------------------------------------------
# IO shapes


class CalendarSourceCreateRequest(BaseModel):
    adapter_type: Literal["ios_eventkit"] = "ios_eventkit"
    external_id: str = Field(..., max_length=256)
    display_name: str = Field(..., max_length=256)
    privacy_mode: PrivacyMode = "title_and_time"
    # Default false — the LLM exposure floor (PM B-4). User must
    # explicitly elevate. UI should surface this as a separate toggle
    # from privacy_mode so the two-elevation model is visible.
    llm_full_details_consent: bool = False


class CalendarSourcePatchRequest(BaseModel):
    privacy_mode: PrivacyMode | None = None
    llm_full_details_consent: bool | None = None
    display_name: str | None = Field(default=None, max_length=256)


class CalendarSourceOut(BaseModel):
    id: str
    adapter_type: str
    external_id: str
    display_name: str
    privacy_mode: str
    llm_full_details_consent: bool
    connected_at: datetime
    disconnected_at: datetime | None


class CalendarIngestRequest(BaseModel):
    calendar_source_id: str
    events: list[IOSEventKitEvent]


class CalendarIngestResponse(BaseModel):
    accepted: int
    tombstoned: int
    privacy_mode_applied: str


class CalendarEventOut(BaseModel):
    id: str
    calendar_source_id: str
    external_id: str
    start_at: datetime
    end_at: datetime
    all_day: bool
    title: str | None
    location: str | None
    notes: str | None
    attendees_count: int | None
    privacy_mode_applied: str


# ---------------------------------------------------------------------------
# Sources


@router.post(
    "/sources",
    response_model=CalendarSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    body: CalendarSourceCreateRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> CalendarSourceOut:
    """Bind (or re-bind) a calendar for the active record.

    Idempotent on ``(user_id, person_record_id, adapter_type,
    external_id)`` — re-picking the same iOS calendar from the
    Settings UI is a no-op upsert that reactivates the source if it
    was previously disconnected.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(CalendarSource.__table__)
        .values(
            id=uuid.uuid4(),
            person_record_id=ctx.active_record_id,
            user_id=ctx.user.id,
            adapter_type=body.adapter_type,
            external_id=body.external_id,
            display_name=body.display_name,
            privacy_mode=body.privacy_mode,
            llm_full_details_consent=body.llm_full_details_consent,
            connected_at=now,
            disconnected_at=None,
        )
        .on_conflict_do_update(
            constraint="calendar_sources_user_record_adapter_external_uq",
            set_={
                "display_name": body.display_name,
                "privacy_mode": body.privacy_mode,
                "llm_full_details_consent": body.llm_full_details_consent,
                "connected_at": now,
                "disconnected_at": None,
                "updated_at": now,
            },
        )
        .returning(CalendarSource.__table__)
    )
    row = (await db.execute(stmt)).mappings().one()
    await db.commit()
    log.info(
        "calendar_source_bound",
        source_id=str(row["id"]),
        adapter_type=row["adapter_type"],
        privacy_mode=row["privacy_mode"],
        llm_full_details_consent=row["llm_full_details_consent"],
        person_record_id=str(ctx.active_record_id),
    )
    return _source_out(row)


@router.get("/sources", response_model=list[CalendarSourceOut])
async def list_sources(
    ctx: AuthContext = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_session),
) -> list[CalendarSourceOut]:
    """Active sources (``disconnected_at IS NULL``) for the active
    record. Disconnected sources are intentionally hidden — once a
    user has disconnected a calendar, they shouldn't have to scroll
    past it in the Settings list."""
    rows = (await db.execute(
        select(CalendarSource)
        .where(CalendarSource.person_record_id == ctx.active_record_id)
        .where(CalendarSource.disconnected_at.is_(None))
        .order_by(CalendarSource.connected_at)
    )).scalars().all()
    return [_source_out_from_row(r) for r in rows]


@router.patch("/sources/{source_id}", response_model=CalendarSourceOut)
async def patch_source(
    source_id: uuid.UUID,
    body: CalendarSourcePatchRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> CalendarSourceOut:
    """Update privacy posture / display name on a source.

    Tightening ``privacy_mode`` (looser → stricter) immediately
    sweeps existing events under this source and redacts the fields
    the new mode forbids. Loosening does NOT retroactively repopulate
    fields — the data was never stored. iOS re-syncs to fill in
    fresher fields under the looser mode.
    """
    src = (await db.execute(
        select(CalendarSource)
        .where(CalendarSource.id == source_id)
        .where(CalendarSource.person_record_id == ctx.active_record_id)
    )).scalar_one_or_none()
    # Cross-record probe → 404 (don't disclose existence).
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    tightening_to: PrivacyMode | None = None
    if body.privacy_mode is not None:
        if _PRIVACY_RANK[body.privacy_mode] < _PRIVACY_RANK[src.privacy_mode]:
            tightening_to = body.privacy_mode
        src.privacy_mode = body.privacy_mode
    if body.llm_full_details_consent is not None:
        src.llm_full_details_consent = body.llm_full_details_consent
    if body.display_name is not None:
        src.display_name = body.display_name
    src.updated_at = datetime.now(timezone.utc)

    if tightening_to is not None:
        await _redact_events_for_tightening(db, src.id, tightening_to)

    await db.commit()
    await db.refresh(src)
    log.info(
        "calendar_source_patched",
        source_id=str(src.id),
        privacy_mode=src.privacy_mode,
        llm_full_details_consent=src.llm_full_details_consent,
        tightening_sweep=tightening_to is not None,
    )
    return _source_out_from_row(src)


async def _redact_events_for_tightening(
    db: AsyncSession,
    source_id: uuid.UUID,
    new_mode: PrivacyMode,
) -> None:
    """Sweep existing events under ``source_id`` and apply the new
    (stricter) privacy mode. Skipped on no-op direction (loosening
    or same-mode); caller checks rank before invoking.

    Tombstoned events are left alone — they're already out of
    retrieval and on the path to the 30d hard-delete.
    """
    if new_mode == "busy_only":
        # busy_only zeros everything user-visible.
        await db.execute(
            update(CalendarEvent)
            .where(CalendarEvent.calendar_source_id == source_id)
            .where(CalendarEvent.tombstoned_at.is_(None))
            .values(
                title=None, location=None, notes=None,
                attendees_count=None,
                privacy_mode_applied="busy_only",
                updated_at=datetime.now(timezone.utc),
            )
        )
    elif new_mode == "title_and_time":
        # title_and_time strips location/notes/attendees from rows
        # that were stored under full_details. busy_only rows are
        # already stricter — leave them as-is.
        await db.execute(
            update(CalendarEvent)
            .where(CalendarEvent.calendar_source_id == source_id)
            .where(CalendarEvent.tombstoned_at.is_(None))
            .where(CalendarEvent.privacy_mode_applied == "full_details")
            .values(
                location=None, notes=None, attendees_count=None,
                privacy_mode_applied="title_and_time",
                updated_at=datetime.now(timezone.utc),
            )
        )
    # full_details cannot be a tightening; caller's rank check
    # prevents reaching here.


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect_source(
    source_id: uuid.UUID,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Soft-disconnect a source and cascade-tombstone its events.

    Tombstones (not hard deletes) leave the rows in place for the
    30-day worker (PM B-3). The user-visible privacy contract is
    satisfied immediately — retrieval filters tombstoned rows — and
    completes physically after the TTL.
    """
    src = (await db.execute(
        select(CalendarSource)
        .where(CalendarSource.id == source_id)
        .where(CalendarSource.person_record_id == ctx.active_record_id)
    )).scalar_one_or_none()
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    now = datetime.now(timezone.utc)
    src.disconnected_at = now
    src.updated_at = now
    await db.execute(
        update(CalendarEvent)
        .where(CalendarEvent.calendar_source_id == src.id)
        .where(CalendarEvent.tombstoned_at.is_(None))
        .values(tombstoned_at=now, updated_at=now)
    )
    await db.commit()
    log.info(
        "calendar_source_disconnected",
        source_id=str(src.id),
        person_record_id=str(ctx.active_record_id),
    )


# ---------------------------------------------------------------------------
# Ingest


@router.post("/ingest", response_model=CalendarIngestResponse)
async def ingest_events(
    body: CalendarIngestRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> CalendarIngestResponse:
    """Batch upsert (or tombstone) events for one source.

    Every UPSERT runs through the server-side redactor regardless of
    what iOS sent — defense in depth for the privacy contract. iOS
    is expected to apply ``privacy_mode`` client-side first; a chatty
    title that crossed the wire when the source is in ``busy_only``
    mode is silently dropped by the server before insert.

    Tombstones (``IOSEventKitEvent.tombstoned=true``) are routed to
    a soft-delete update; the row stays for the 30-day purge.
    """
    try:
        source_uuid = uuid.UUID(body.calendar_source_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    src = (await db.execute(
        select(CalendarSource)
        .where(CalendarSource.id == source_uuid)
        .where(CalendarSource.person_record_id == ctx.active_record_id)
        .where(CalendarSource.disconnected_at.is_(None))
    )).scalar_one_or_none()
    if src is None:
        # Cross-record probe OR disconnected source → 404 either way.
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    accepted = 0
    tombstoned_count = 0
    now = datetime.now(timezone.utc)

    for ev in body.events:
        redacted = redact_event_for_storage(ev, privacy_mode=src.privacy_mode)
        if redacted["tombstoned"]:
            await db.execute(
                update(CalendarEvent)
                .where(CalendarEvent.calendar_source_id == src.id)
                .where(CalendarEvent.external_id == redacted["external_id"])
                .where(CalendarEvent.tombstoned_at.is_(None))
                .values(tombstoned_at=now, updated_at=now)
            )
            tombstoned_count += 1
            continue

        await db.execute(
            pg_insert(CalendarEvent.__table__)
            .values(
                id=uuid.uuid4(),
                person_record_id=ctx.active_record_id,
                calendar_source_id=src.id,
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
                    # Un-tombstone if the event came back. iOS re-emits
                    # an event after the user un-deletes it in Calendar.app.
                    "tombstoned_at": None,
                    "updated_at": now,
                },
            )
        )
        accepted += 1

    await db.commit()
    log.info(
        "calendar_ingest_batch",
        source_id=str(src.id),
        accepted=accepted,
        tombstoned=tombstoned_count,
        privacy_mode_applied=src.privacy_mode,
        person_record_id=str(ctx.active_record_id),
    )
    return CalendarIngestResponse(
        accepted=accepted,
        tombstoned=tombstoned_count,
        privacy_mode_applied=src.privacy_mode,
    )


# ---------------------------------------------------------------------------
# Events listing (UI surface — full stored fields per privacy_mode_applied)


@router.get("/events", response_model=list[CalendarEventOut])
async def list_events(
    start_at: datetime = Query(...),
    end_at: datetime = Query(...),
    limit: int = Query(default=100, le=500),
    ctx: AuthContext = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_session),
) -> list[CalendarEventOut]:
    """Active (non-tombstoned) events overlapping the requested
    time window, scoped to the active record. Returns what's STORED
    (subject to ``privacy_mode_applied``) — the user is allowed to
    see their own data verbatim. Ask retrieval uses
    ``project_event_for_llm`` instead, which applies the second
    elevation.
    """
    if end_at <= start_at:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="end_at must be after start_at",
        )
    rows = (await db.execute(
        select(CalendarEvent)
        .where(CalendarEvent.person_record_id == ctx.active_record_id)
        .where(CalendarEvent.tombstoned_at.is_(None))
        .where(CalendarEvent.start_at < end_at)
        .where(CalendarEvent.end_at > start_at)
        .order_by(CalendarEvent.start_at)
        .limit(limit)
    )).scalars().all()
    return [
        CalendarEventOut(
            id=str(r.id),
            calendar_source_id=str(r.calendar_source_id),
            external_id=r.external_id,
            start_at=r.start_at,
            end_at=r.end_at,
            all_day=r.all_day,
            title=r.title,
            location=r.location,
            notes=r.notes,
            attendees_count=r.attendees_count,
            privacy_mode_applied=r.privacy_mode_applied,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helpers


def _source_out(row) -> CalendarSourceOut:
    """Map a row-mapping (from .returning(table)) to the response."""
    return CalendarSourceOut(
        id=str(row["id"]),
        adapter_type=row["adapter_type"],
        external_id=row["external_id"],
        display_name=row["display_name"],
        privacy_mode=row["privacy_mode"],
        llm_full_details_consent=row["llm_full_details_consent"],
        connected_at=row["connected_at"],
        disconnected_at=row["disconnected_at"],
    )


def _source_out_from_row(r: CalendarSource) -> CalendarSourceOut:
    """Map a CalendarSource ORM row to the response."""
    return CalendarSourceOut(
        id=str(r.id),
        adapter_type=r.adapter_type,
        external_id=r.external_id,
        display_name=r.display_name,
        privacy_mode=r.privacy_mode,
        llm_full_details_consent=r.llm_full_details_consent,
        connected_at=r.connected_at,
        disconnected_at=r.disconnected_at,
    )
