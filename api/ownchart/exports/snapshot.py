"""Slice 4 export snapshot builder.

A read-only, record-scoped point-in-time gather of everything the
two Slice-4 mappers (canonical JSON + human TXT) need to render an
export. Defined as a Pydantic model so:

  - the contract between the worker (which builds the snapshot) and
    the mappers (which read it) is explicit and typed
  - the mappers can be unit-tested against synthetic snapshots
    without touching the DB
  - future mappers (Pictal JSON in M03, CCDA later) can be added by
    reading the same snapshot without re-querying

The Slice-4 minimum bundles only the fields the two mappers actually
print or serialize. Adding new fields here is an additive change —
mappers MAY ignore fields they don't use, and a future field MUST
be optional to keep snapshot construction backward compatible with
the JSON-on-disk cache (when we add one in M03+).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Snapshot sub-shapes


class _SnapshotRecord(BaseModel):
    """The person_record metadata bundled into the export. iOS UI
    surfaces `display_name`; the others are present for completeness
    so re-importing a JSON export round-trips."""
    id: str
    display_name: str
    given_names: str | None = None
    family_name: str | None = None
    birth_date: date | None = None
    gender: str | None = None
    is_self: bool = False


class _SnapshotSource(BaseModel):
    """One source_document row for the record. Mappers use
    `source_label` + `acquired_at` for the human-readable section
    headings; UI / re-import uses the id + metadata."""
    id: str
    source_type: str
    source_label: str | None = None
    source_system: str | None = None
    original_filename: str | None = None
    acquired_at: datetime | None = None
    created_at: datetime


class _SnapshotFact(BaseModel):
    """One extracted_fact row scoped to the record. Mappers render
    `label`, the date pair, and significance. Confidence + review
    state included so a re-import can preserve user-confirmation
    state."""
    id: str
    fact_type: str
    label: str
    description: str | None = None
    date_start: datetime | None = None
    date_end: datetime | None = None
    date_precision: str | None = None
    coded_concepts: dict[str, Any] | None = None
    confidence: int | None = None
    review_state: str
    significance: str | None = None
    significance_source: str | None = None
    created_at: datetime


class _SnapshotCalendarSource(BaseModel):
    """One calendar_sources row. Privacy posture included so the
    re-import can re-create the same source under the same mode."""
    id: str
    adapter_type: str
    display_name: str
    privacy_mode: str
    llm_full_details_consent: bool
    connected_at: datetime
    disconnected_at: datetime | None = None


class _SnapshotCalendarEvent(BaseModel):
    """One calendar_events row. Tombstoned rows are EXCLUDED from
    the snapshot — they're on the path to hard delete and shouldn't
    surface in an export the user reads."""
    id: str
    calendar_source_id: str
    title: str | None = None
    location: str | None = None
    notes: str | None = None
    attendees_count: int | None = None
    start_at: datetime
    end_at: datetime
    all_day: bool
    privacy_mode_applied: str


class ExportSnapshot(BaseModel):
    """The full record-scoped bundle that mappers consume.

    All collections are scoped to `record.id` — every row in
    `sources`, `facts`, `calendar_sources`, `calendar_events` has
    `person_record_id = record.id` at snapshot build time. The
    mapper layer trusts this and does not re-check.

    `generated_at` is the snapshot's wall-clock at build time. A
    re-import of the JSON should treat this as the export's "as-of"
    timestamp — facts created later in the source DB won't appear.
    """

    snapshot_version: str = Field(default="1.0")
    generated_at: datetime
    record: _SnapshotRecord
    sources: list[_SnapshotSource] = Field(default_factory=list)
    facts: list[_SnapshotFact] = Field(default_factory=list)
    calendar_sources: list[_SnapshotCalendarSource] = Field(default_factory=list)
    calendar_events: list[_SnapshotCalendarEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Builder


async def build_export_snapshot(
    db: AsyncSession,
    *,
    person_record_id: uuid.UUID,
    now: datetime | None = None,
) -> ExportSnapshot:
    """Gather all record-scoped data for ``person_record_id`` into a
    snapshot. Read-only — no DB writes. Filters every collection by
    ``person_record_id`` to honor the Slice 1 perimeter contract.

    Tombstoned calendar events are EXCLUDED — exports should reflect
    the user's current view, not the soft-delete shadow that lives
    until the 30d purge. Soft-deleted sources / facts (if such a
    concept existed) would similarly be excluded; today none of the
    other models have a tombstone column, so the only filter needed
    here is the calendar one.
    """
    from datetime import timezone as _tz

    from ..models.calendar_event import CalendarEvent
    from ..models.calendar_source import CalendarSource
    from ..models.extracted_fact import ExtractedFact
    from ..models.person_record import PersonRecord
    from ..models.source_document import SourceDocument

    record = (await db.execute(
        select(PersonRecord).where(PersonRecord.id == person_record_id)
    )).scalar_one()

    sources = (await db.execute(
        select(SourceDocument)
        .where(SourceDocument.person_record_id == person_record_id)
        .order_by(SourceDocument.created_at.asc())
    )).scalars().all()

    facts = (await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.person_record_id == person_record_id)
        .order_by(ExtractedFact.date_start.asc().nullslast(),
                  ExtractedFact.created_at.asc())
    )).scalars().all()

    cal_sources = (await db.execute(
        select(CalendarSource)
        .where(CalendarSource.person_record_id == person_record_id)
        .order_by(CalendarSource.connected_at.asc())
    )).scalars().all()

    cal_events = (await db.execute(
        select(CalendarEvent)
        .where(CalendarEvent.person_record_id == person_record_id)
        .where(CalendarEvent.tombstoned_at.is_(None))
        .order_by(CalendarEvent.start_at.asc())
    )).scalars().all()

    return ExportSnapshot(
        generated_at=(now or datetime.now(_tz.utc)),
        record=_SnapshotRecord(
            id=str(record.id),
            display_name=record.display_name,
            given_names=record.given_names,
            family_name=record.family_name,
            birth_date=record.birth_date,
            gender=record.gender,
            is_self=record.is_self,
        ),
        sources=[
            _SnapshotSource(
                id=str(s.id),
                source_type=s.source_type,
                source_label=s.source_label,
                source_system=s.source_system,
                original_filename=s.original_filename,
                acquired_at=s.acquired_at,
                created_at=s.created_at,
            )
            for s in sources
        ],
        facts=[
            _SnapshotFact(
                id=str(f.id),
                fact_type=f.fact_type,
                label=f.label,
                description=f.description,
                date_start=f.date_start,
                date_end=f.date_end,
                date_precision=f.date_precision,
                coded_concepts=f.coded_concepts,
                confidence=f.confidence,
                review_state=f.review_state,
                significance=f.significance,
                significance_source=f.significance_source,
                created_at=f.created_at,
            )
            for f in facts
        ],
        calendar_sources=[
            _SnapshotCalendarSource(
                id=str(cs.id),
                adapter_type=cs.adapter_type,
                display_name=cs.display_name,
                privacy_mode=cs.privacy_mode,
                llm_full_details_consent=cs.llm_full_details_consent,
                connected_at=cs.connected_at,
                disconnected_at=cs.disconnected_at,
            )
            for cs in cal_sources
        ],
        calendar_events=[
            _SnapshotCalendarEvent(
                id=str(ce.id),
                calendar_source_id=str(ce.calendar_source_id),
                title=ce.title,
                location=ce.location,
                notes=ce.notes,
                attendees_count=ce.attendees_count,
                start_at=ce.start_at,
                end_at=ce.end_at,
                all_day=ce.all_day,
                privacy_mode_applied=ce.privacy_mode_applied,
            )
            for ce in cal_events
        ],
    )
