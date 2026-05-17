"""Auto-association for personal uploads.

When a patient uploads a photo, note, or voice memo with a captured-at
date, look around that date (±7 days) for major clinical events
already in the record. Stash a small index of those on the upload's
SourceDocument so the source detail page can show "this is on the
same day as your appendectomy / fibula fracture" without the patient
having to manually link the upload to anything.

Doctrine: significance-based ranking (not fact count, not source
density). We only surface facts with significance in {major_event,
major_procedure, major_diagnosis, major_medication} — background
chatter doesn't make for useful context.

V1 stores the discovered events in `source.raw_metadata['nearby_clinical_events']`
as a list of `{fact_id, label, date, fact_type, significance}` dicts.
The source detail page (and future review-inbox candidates) reads
that list. No new tables, no new candidate workflow — that can come
in V1.1 if it earns the complexity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User


_MAJOR_SIGNIFICANCE = (
    "major_event",
    "major_procedure",
    "major_diagnosis",
    "major_medication",
)


async def attach_nearby_clinical_events(
    db: AsyncSession,
    user: User,
    source: SourceDocument,
    *,
    window_days: int = 7,
    max_events: int = 8,
) -> list[dict]:
    """Find major clinical facts within ±window_days of source.captured_at
    (or user_supplied_event_date, or now). Store on source.raw_metadata
    so the UI can render them. Returns the list it stored.

    Safe to call multiple times — replaces the stored list each call.
    Returns empty list when the source has no date anchor (we don't
    invent context for undated uploads).
    """
    anchor_date = (
        source.captured_at
        or source.user_supplied_event_date
        or source.acquired_at
    )
    if anchor_date is None:
        return []

    if anchor_date.tzinfo is None:
        anchor_date = anchor_date.replace(tzinfo=timezone.utc)

    window_start = anchor_date - timedelta(days=window_days)
    window_end = anchor_date + timedelta(days=window_days)

    # Per M02 perimeter (BE-2): scope by the source's
    # person_record_id, not the caller's user_id. Caregivers can
    # upload to a parent's record; the nearby-events lookup must
    # see THAT record's history, not the caregiver's own. The
    # `user` argument is retained for the signature's existing
    # callers but no longer load-bearing for scoping.
    user_scope = (
        exists()
        .where(EvidenceAnchor.id == ExtractedFact.evidence_anchor_ids.any_())
        .where(SourceDocument.id == EvidenceAnchor.source_document_id)
        .where(SourceDocument.person_record_id == source.person_record_id)
        .where(SourceDocument.id != source.id)
    )
    q = (
        select(ExtractedFact)
        .where(user_scope)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= window_start)
        .where(ExtractedFact.date_start <= window_end)
        # "Reviewed" = user has acknowledged this fact (confirmed it,
        # corrected it, or accepted its containing pattern). All three
        # count for grounding the extractor's view of the user's record.
        .where(ExtractedFact.review_state.in_(("confirmed", "corrected", "pattern_managed")))
        .where(ExtractedFact.significance.in_(_MAJOR_SIGNIFICANCE))
        .order_by(ExtractedFact.date_start.asc())
        .limit(max_events * 3)  # over-fetch; we'll dedupe by equivalence_key
    )
    facts_in_window = list((await db.execute(q)).scalars().all())

    # Fetch the source label for each fact's primary anchor (best-effort).
    anchor_to_label: dict = {}
    if facts_in_window:
        all_anchor_ids: list = []
        for f in facts_in_window:
            for aid in (f.evidence_anchor_ids or []):
                all_anchor_ids.append(aid)
        if all_anchor_ids:
            label_rows = (await db.execute(
                select(EvidenceAnchor.id, SourceDocument.source_label)
                .join(SourceDocument, SourceDocument.id == EvidenceAnchor.source_document_id)
                .where(EvidenceAnchor.id.in_(all_anchor_ids))
            )).all()
            anchor_to_label = {aid: lbl for aid, lbl in label_rows}

    rows = [
        (f, anchor_to_label.get((f.evidence_anchor_ids or [None])[0]))
        for f in facts_in_window
    ]

    # Dedup by equivalence_key (same canonical event from multiple
    # sources collapses to one entry). Falls back to label+date.
    seen_keys: set[str] = set()
    events: list[dict] = []
    for fact, src_label in rows:
        key = fact.equivalence_key or f"{(fact.label or '').lower().strip()}|{fact.date_start.date().isoformat() if fact.date_start else ''}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        events.append({
            "fact_id": str(fact.id),
            "label": fact.label,
            "date": fact.date_start.date().isoformat() if fact.date_start else None,
            "fact_type": fact.fact_type,
            "significance": fact.significance,
            "source_label": src_label,
            "days_offset": (fact.date_start.date() - anchor_date.date()).days
                if fact.date_start else None,
        })
        if len(events) >= max_events:
            break

    raw = dict(source.raw_metadata or {})
    raw["nearby_clinical_events"] = events
    source.raw_metadata = raw

    return events
