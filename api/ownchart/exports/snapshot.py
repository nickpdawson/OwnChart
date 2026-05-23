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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Section D — request-time filter envelope. Pure-function resolver
# below converts the on-job JSONB into a concrete date window + a
# domain set the SQL queries apply.

# Auto-export + native HealthKit are the two extraction_method values
# that produce "body signals / measured health data" facts. Anything
# else is treated as clinical for the domain filter. CCDA, vision,
# FHIR resources, manual notes — all clinical.
_BODY_SIGNAL_METHODS = frozenset({"health_auto_export", "native_healthkit"})


@dataclass(frozen=True)
class ResolvedFilters:
    """Resolved snapshot filter envelope. Output of `resolve_filters`,
    consumed by `build_export_snapshot`.

    `date_start` / `date_end` are inclusive UTC bounds; either may be
    None meaning "no bound on that end." `include_clinical` etc.
    drive which collections are pulled into the snapshot at all.
    """
    date_start: datetime | None
    date_end: datetime | None
    include_clinical: bool
    include_body_signals: bool
    include_calendar: bool


def resolve_filters(
    raw: dict | None,
    *,
    now: datetime | None = None,
) -> ResolvedFilters:
    """Convert the on-job filter envelope into concrete window + flags.

    Pure-function — tested separately from the DB-touching builder.
    `raw=None` (pre-Section-D job, or unfiltered request) yields the
    full-record default: no date bounds, all three domains included.

    Validation:
      - date_range_kind must be one of {'all', 'last_90d', 'last_1y',
        'custom'}; anything else collapses to 'all' (defensive — the
        Pydantic Literal on CreateExportRequest already rejects bad
        values upstream, but the runner may load a corrupted JSON
        from a future hand-written job).
      - For 'custom', date_range_start is honored; date_range_end
        defaults to `now` if absent. If start > end, the window is
        swapped so the snapshot doesn't return zero rows.
    """
    now_dt = now or datetime.now(timezone.utc)
    if not isinstance(raw, dict):
        return ResolvedFilters(
            date_start=None,
            date_end=None,
            include_clinical=True,
            include_body_signals=True,
            include_calendar=True,
        )

    kind = raw.get("date_range_kind") or "all"
    if kind not in ("all", "last_90d", "last_1y", "custom"):
        kind = "all"

    date_start: datetime | None = None
    date_end: datetime | None = None
    if kind == "last_90d":
        date_start = now_dt - timedelta(days=90)
    elif kind == "last_1y":
        date_start = now_dt - timedelta(days=365)
    elif kind == "custom":
        rs = raw.get("date_range_start")
        re_ = raw.get("date_range_end")
        date_start = _parse_iso(rs)
        date_end = _parse_iso(re_) or now_dt
        if date_start is not None and date_end is not None and date_start > date_end:
            date_start, date_end = date_end, date_start

    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, list) or not domains_raw:
        domains = {"clinical", "body_signals", "calendar"}
    else:
        domains = {str(d) for d in domains_raw}

    return ResolvedFilters(
        date_start=date_start,
        date_end=date_end,
        include_clinical="clinical" in domains,
        include_body_signals="body_signals" in domains,
        include_calendar="calendar" in domains,
    )


def _parse_iso(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        out = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Treat naive timestamps as UTC so downstream comparisons are sane.
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out


def fact_method_is_body_signal(extraction_method: str | None) -> bool:
    """Pure helper for the domain filter. Exposed so tests can pin the
    membership without re-importing the private set."""
    return (extraction_method or "") in _BODY_SIGNAL_METHODS


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
    filters: dict | None = None,
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

    Section D Phase 1 — ``filters`` is the request-time envelope
    persisted on the ExportJob row. None means "no filters, full
    record" (the pre-Section-D default). When supplied:

      - date_range_kind drives a date_start window on ExtractedFact
        + CalendarEvent (sources are NOT date-filtered; an export
        with no dated facts in window still wants to be honest about
        what was ingested).
      - domains={'clinical','body_signals','calendar'} drives which
        collections are pulled. Omitted domains yield empty lists,
        not missing keys, so JSON shape stays stable.
    """
    from datetime import timezone as _tz

    from ..models.calendar_event import CalendarEvent
    from ..models.calendar_source import CalendarSource
    from ..models.extracted_fact import ExtractedFact
    from ..models.person_record import PersonRecord
    from ..models.source_document import SourceDocument

    f = resolve_filters(filters, now=now)

    record = (await db.execute(
        select(PersonRecord).where(PersonRecord.id == person_record_id)
    )).scalar_one()

    # Sources are always pulled — they're the provenance for any fact
    # the user kept, regardless of the domain filter. Skipping them
    # would make the JSON misleading about where the data came from.
    sources = (await db.execute(
        select(SourceDocument)
        .where(SourceDocument.person_record_id == person_record_id)
        .order_by(SourceDocument.created_at.asc())
    )).scalars().all()

    # Facts: apply date window + domain filter.
    facts: list = []
    if f.include_clinical or f.include_body_signals:
        fact_q = (
            select(ExtractedFact)
            .where(ExtractedFact.person_record_id == person_record_id)
            .order_by(ExtractedFact.date_start.asc().nullslast(),
                      ExtractedFact.created_at.asc())
        )
        if f.date_start is not None:
            # Inclusive: keep facts whose date_start >= window start.
            # Also keep NULL date_start when filtering by 'all' (no
            # window). NULL-date facts are excluded from windowed
            # filters since we can't honestly place them in the range.
            fact_q = fact_q.where(ExtractedFact.date_start >= f.date_start)
        if f.date_end is not None:
            fact_q = fact_q.where(ExtractedFact.date_start <= f.date_end)
        # Domain filter at the SQL layer when only one domain is on;
        # both-on means no method filter at all (cheaper than IN).
        if f.include_clinical and not f.include_body_signals:
            fact_q = fact_q.where(
                ~ExtractedFact.extraction_method.in_(_BODY_SIGNAL_METHODS)
            )
        elif f.include_body_signals and not f.include_clinical:
            fact_q = fact_q.where(
                ExtractedFact.extraction_method.in_(_BODY_SIGNAL_METHODS)
            )
        facts = list((await db.execute(fact_q)).scalars().all())

    # Calendar sources + events only when domain is on.
    if f.include_calendar:
        cal_sources_q = (
            select(CalendarSource)
            .where(CalendarSource.person_record_id == person_record_id)
            .order_by(CalendarSource.connected_at.asc())
        )
        cal_sources = list(
            (await db.execute(cal_sources_q)).scalars().all()
        )

        cal_events_q = (
            select(CalendarEvent)
            .where(CalendarEvent.person_record_id == person_record_id)
            .where(CalendarEvent.tombstoned_at.is_(None))
            .order_by(CalendarEvent.start_at.asc())
        )
        if f.date_start is not None:
            cal_events_q = cal_events_q.where(
                CalendarEvent.start_at >= f.date_start
            )
        if f.date_end is not None:
            cal_events_q = cal_events_q.where(
                CalendarEvent.start_at <= f.date_end
            )
        cal_events = list((await db.execute(cal_events_q)).scalars().all())
    else:
        cal_sources = []
        cal_events = []

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
