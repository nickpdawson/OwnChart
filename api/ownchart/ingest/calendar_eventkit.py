"""EventKit calendar ingest — iOS adapter, privacy redaction, LLM
exposure projection, and the 30-day tombstone purge.

This is the iOS-only Slice 3 implementation. Google / ICS / CalDAV
are out of scope; ``ADAPTER_TYPES`` in ``models.calendar_source``
reserves their names so they can land without touching this layer.

Two privacy boundaries, both enforced here:

  1. **REDACT AT INGEST (defense in depth).** The route layer NEVER
     trusts iOS to apply ``privacy_mode`` correctly.
     ``redact_event_for_storage`` is the authoritative redactor that
     runs before any UPSERT. iOS is expected to apply the mode
     client-side too — so a chatty title never crosses the wire
     when ``busy_only`` is the source's mode — but the server is
     the final word.

  2. **PROJECT AT RETRIEVAL (LLM exposure floor).**
     ``project_event_for_llm`` is what Ask retrieval calls before
     any event lands in a prompt. The "second elevation" (PM B-4)
     is ``source_consent``: even when full fields are stored, the
     projector hides them unless the user elevated
     ``llm_full_details_consent=true`` on the owning source. The
     default — busy-only-equivalent to the LLM — is the floor.

Both functions are pure and called from the route layer + tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

PrivacyMode = Literal["full_details", "title_and_time", "busy_only"]
PRIVACY_MODES_TUPLE: tuple[PrivacyMode, ...] = (
    "full_details", "title_and_time", "busy_only",
)


# ---------------------------------------------------------------------------
# Wire shape — what iOS sends


class IOSEventKitEvent(BaseModel):
    """One event from the iOS EventKit batch ingest payload.

    iOS is expected to apply the source's ``privacy_mode`` client-side
    (so e.g. ``title`` is omitted when the mode is ``busy_only``),
    but every field here is server-side optional and the redactor
    re-applies the mode authoritatively.
    """

    external_id: str = Field(..., max_length=256)
    external_modified_at: datetime
    start_at: datetime
    end_at: datetime
    all_day: bool = False
    title: str | None = Field(default=None, max_length=512)
    location: str | None = Field(default=None, max_length=512)
    notes: str | None = None
    attendees_count: int | None = Field(default=None, ge=0)
    # iOS-side provenance: recurrence pattern, EventKit calendar
    # color, attendee role, etc. Opaque to retrieval — stored as
    # ``calendar_events.raw_metadata`` so future debug paths can
    # see what iOS recorded without re-querying the device.
    metadata: dict[str, Any] | None = None
    # iOS-side delete signal: when true, server marks the existing
    # row tombstoned rather than upserting fresh fields. iOS may
    # batch a tombstoned=true alongside fresh events when a calendar
    # has both modified-and-deleted-elsewhere events in the same
    # window.
    tombstoned: bool = False


# ---------------------------------------------------------------------------
# Privacy redaction at ingest (defense in depth)


_USER_VISIBLE_FIELDS = ("title", "location", "notes", "attendees_count")


def redact_event_for_storage(
    event: IOSEventKitEvent | dict[str, Any],
    *,
    privacy_mode: PrivacyMode,
) -> dict[str, Any]:
    """Apply ``privacy_mode`` at the server. Authoritative redactor.

    Returns a dict suitable for the UPSERT — the four user-visible
    fields (``title``, ``location``, ``notes``, ``attendees_count``)
    are forced to ``None`` when the mode forbids them. Mode-by-field:

      busy_only       →  none of the four kept; row existence is the
                         "busy" signal.
      title_and_time  →  title kept; location, notes, attendees zeroed.
      full_details    →  all four kept as iOS sent them.

    ``raw_metadata`` is preserved across all modes (it's iOS-side
    provenance — recurrence pattern, EK color — not user content).
    If iOS were to smuggle user content into metadata, that would be
    an iOS bug; the server can't validate every metadata key.

    ``privacy_mode_applied`` is stamped onto the returned dict so the
    DB row records the redaction decision and a later sweep can find
    rows that need to be re-redacted when the source's privacy_mode
    tightens.
    """
    if privacy_mode not in PRIVACY_MODES_TUPLE:
        raise ValueError(
            f"unknown privacy_mode: {privacy_mode!r}; "
            f"must be one of {PRIVACY_MODES_TUPLE}"
        )

    if isinstance(event, IOSEventKitEvent):
        e: IOSEventKitEvent = event
    else:
        e = IOSEventKitEvent.model_validate(event)

    base: dict[str, Any] = {
        "external_id": e.external_id,
        "external_modified_at": e.external_modified_at,
        "start_at": e.start_at,
        "end_at": e.end_at,
        "all_day": e.all_day,
        "privacy_mode_applied": privacy_mode,
        "tombstoned": e.tombstoned,
        "raw_metadata": e.metadata,
    }

    if privacy_mode == "busy_only":
        for k in _USER_VISIBLE_FIELDS:
            base[k] = None
    elif privacy_mode == "title_and_time":
        base["title"] = e.title
        base["location"] = None
        base["notes"] = None
        base["attendees_count"] = None
    else:  # full_details
        base["title"] = e.title
        base["location"] = e.location
        base["notes"] = e.notes
        base["attendees_count"] = e.attendees_count

    return base


# ---------------------------------------------------------------------------
# LLM exposure floor (PM B-4)


def project_event_for_llm(
    *,
    start_at: datetime,
    end_at: datetime,
    all_day: bool,
    title: str | None,
    location: str | None,
    notes: str | None,
    attendees_count: int | None,
    privacy_mode_applied: PrivacyMode,
    source_consent: bool,
) -> dict[str, Any]:
    """Choose what to expose to the LLM (Ask retrieval projection).

    Two-axis decision:

      axis 1 — what's STORED on the row, controlled by
               ``privacy_mode_applied`` (the redaction at ingest).
      axis 2 — what the user has APPROVED the LLM to see,
               controlled by ``source_consent``
               (``calendar_sources.llm_full_details_consent``).

    The projection is the intersection: floor at busy_only-equivalent
    when ``source_consent is False`` regardless of what's stored.
    Even a row stored under ``full_details`` exposes only
    ``start_at`` / ``end_at`` / ``all_day`` to the LLM if the user
    has not granted ``llm_full_details_consent`` on the owning
    source.

    This is the "second elevation": ``privacy_mode`` controls
    STORAGE, ``llm_full_details_consent`` controls LLM VISIBILITY.
    A user can hold ``title_and_time`` storage + consent=false
    (= LLM sees busy-only) or ``title_and_time`` storage +
    consent=true (= LLM sees title-and-time-equivalent).
    """
    base = {
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
    }
    if not source_consent:
        return base  # LLM exposure floor — busy_only-equivalent
    if privacy_mode_applied == "busy_only":
        return base  # nothing more stored to expose
    base["title"] = title
    if privacy_mode_applied == "title_and_time":
        return base  # no location / notes / attendees stored
    # full_details + consent
    base["location"] = location
    base["notes"] = notes
    base["attendees_count"] = attendees_count
    return base


# ---------------------------------------------------------------------------
# 30-day tombstone purge (PM B-3)


PURGE_TTL_DAYS_DEFAULT = 30


def compute_purge_cutoff(
    *, ttl_days: int = PURGE_TTL_DAYS_DEFAULT, now: datetime | None = None,
) -> datetime:
    """Return the datetime threshold below which a tombstoned event
    is eligible for hard delete. Pure helper; injectable ``now`` for
    tests.
    """
    return (now or datetime.now(timezone.utc)) - timedelta(days=ttl_days)


async def purge_tombstoned_calendar_events(
    db: Any,
    *,
    ttl_days: int = PURGE_TTL_DAYS_DEFAULT,
    now: datetime | None = None,
) -> int:
    """Hard-delete ``calendar_events`` rows that were tombstoned more
    than ``ttl_days`` ago. Returns the count deleted.

    Intended to run periodically (PM B-3, ~daily). Soft-delete
    (``tombstoned_at`` set) is the user-facing disconnect signal;
    hard-delete completes the privacy contract after the TTL.

    Local imports of SQLAlchemy + model to keep this module
    importable in pure-function tests that never wire a DB.
    """
    from sqlalchemy import delete
    from ..models.calendar_event import CalendarEvent

    cutoff = compute_purge_cutoff(ttl_days=ttl_days, now=now)
    result = await db.execute(
        delete(CalendarEvent).where(
            CalendarEvent.tombstoned_at.is_not(None),
            CalendarEvent.tombstoned_at < cutoff,
        )
    )
    return result.rowcount or 0
