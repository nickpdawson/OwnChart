"""Fact list, correction (UserAssertion), and review-inbox endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, get_auth_context, require_role
from ..core.db import get_session
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user_assertion import UserAssertion

router = APIRouter()


class FactDetail(BaseModel):
    id: str
    fact_type: str
    label: str
    description: str | None
    date_start: datetime | None
    date_end: datetime | None
    date_precision: str | None
    body_site: str | None
    laterality: str | None
    confidence: int | None
    review_state: str
    extraction_method: str
    evidence_anchor_ids: list[str]
    canonical_label: str | None
    canonical_description: str | None
    canonical_date_start: datetime | None
    canonical_date_end: datetime | None
    # Source attribution — derived from the first evidence anchor's
    # source_document. The review-inbox lane split (#54) groups
    # provider/contact facts by source for the "Defer all from this
    # source" bulk action; needs source_id + a human-friendly label.
    source_id: str | None = None
    source_label: str | None = None
    source_type: str | None = None
    # Review reasons (docs/07 Priority 1) — the Review Inbox renders
    # `why_needs_review_text` as inline italic copy under each row,
    # and shows a "Mark source-only" quick action when
    # `source_context_only_eligible` is true.
    why_needs_review_code: str | None = None
    why_needs_review_text: str | None = None
    review_priority: int | None = None
    review_task_type: str | None = None
    source_context_only_eligible: bool = False
    # docs/07 R5 — patient-readable candidate label; original `label`
    # is preserved untouched as source-of-truth. UI prefers
    # display_label when present.
    display_label: str | None = None
    display_label_method: str | None = None
    # User-confirmable significance (2026-05-11). Ranks every patient-
    # facing surface. `significance_source` records who set it; user
    # always wins over llm/heuristic/default.
    significance: str | None = None
    significance_source: str | None = None
    # Section C Phase 1 — surfaces drive different copy depending on
    # how the date was derived. 'explicit' = source carried an
    # occurrence date; 'encounter_proximate' = inherited from a linked
    # visit; 'issued_approximate' = from a report-issued timestamp;
    # 'user_canonical' = user-overridden; NULL = no date at all.
    date_provenance: str | None = None
    # FHIR Condition lifecycle: 'resolved' / 'inactive' / 'remission'.
    # Small low-contrast pill in the UI; fact stays retrievable.
    historical_status: str | None = None


class CorrectionRequest(BaseModel):
    # one of: confirm | correct | reject | annotate
    assertion_type: str
    canonical_label: str | None = None
    canonical_description: str | None = None
    canonical_date_start: datetime | None = None
    canonical_date_end: datetime | None = None
    reason: str | None = None
    new_review_state: str | None = None  # confirmed | corrected | rejected | deferred


def _detail(
    c: ExtractedFact,
    ua: UserAssertion | None,
    source: SourceDocument | None = None,
) -> FactDetail:
    return FactDetail(
        id=str(c.id),
        fact_type=c.fact_type,
        label=c.label,
        description=c.description,
        date_start=c.date_start,
        date_end=c.date_end,
        date_precision=c.date_precision,
        body_site=c.body_site,
        laterality=c.laterality,
        confidence=c.confidence,
        review_state=c.review_state,
        extraction_method=c.extraction_method,
        evidence_anchor_ids=[str(x) for x in (c.evidence_anchor_ids or [])],
        canonical_label=ua.canonical_label if ua else None,
        canonical_description=ua.canonical_description if ua else None,
        canonical_date_start=ua.canonical_date_start if ua else None,
        canonical_date_end=ua.canonical_date_end if ua else None,
        source_id=str(source.id) if source else None,
        source_label=(source.source_label or source.original_filename) if source else None,
        source_type=source.source_type if source else None,
        why_needs_review_code=c.why_needs_review_code,
        why_needs_review_text=c.why_needs_review_text,
        review_priority=c.review_priority,
        review_task_type=c.review_task_type,
        source_context_only_eligible=c.source_context_only_eligible,
        display_label=c.display_label,
        display_label_method=c.display_label_method,
        significance=c.significance,
        significance_source=c.significance_source,
        # Section C Phase 1 — if the user has confirmed a canonical
        # date via UserAssertion, surface as 'user_canonical' over
        # the stored extraction provenance.
        date_provenance=(
            "user_canonical" if (ua and ua.canonical_date_start)
            else c.date_provenance
        ),
        historical_status=c.historical_status,
    )


async def _resolve_source_for_facts(
    db: AsyncSession,
    facts: list[ExtractedFact],
) -> dict[uuid.UUID, SourceDocument]:
    """Resolve fact_id → SourceDocument via the first evidence anchor.

    One-shot batch fetch (anchors then sources) so the review inbox
    can render `source_label` on every row without an N+1.
    """
    first_anchors: list[uuid.UUID] = []
    for f in facts:
        if f.evidence_anchor_ids:
            first_anchors.append(f.evidence_anchor_ids[0])
    if not first_anchors:
        return {}
    anc_q = await db.execute(
        select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
        .where(EvidenceAnchor.id.in_(first_anchors))
    )
    anchor_to_source_id: dict[uuid.UUID, uuid.UUID] = {
        aid: sid for (aid, sid) in anc_q.all() if sid is not None
    }
    src_ids = list(set(anchor_to_source_id.values()))
    if not src_ids:
        return {}
    src_q = await db.execute(
        select(SourceDocument).where(SourceDocument.id.in_(src_ids))
    )
    src_by_id: dict[uuid.UUID, SourceDocument] = {s.id: s for s in src_q.scalars().all()}

    out: dict[uuid.UUID, SourceDocument] = {}
    for f in facts:
        first = (f.evidence_anchor_ids or [None])[0]
        if first is None:
            continue
        sid = anchor_to_source_id.get(first)
        if sid is None:
            continue
        s = src_by_id.get(sid)
        if s is not None:
            out[f.id] = s
    return out


async def _latest_assertion(db: AsyncSession, fact_id: uuid.UUID) -> UserAssertion | None:
    q = await db.execute(
        select(UserAssertion)
        .where(UserAssertion.related_fact_id == fact_id)
        .order_by(UserAssertion.created_at.desc())
        .limit(1)
    )
    return q.scalar_one_or_none()


@router.get("")
async def list_facts(
    review_state: str | None = Query(default=None),
    fact_type: str | None = Query(default=None),
    q: str | None = Query(default=None, description="free-text label/description filter"),
    source_id: uuid.UUID | None = Query(default=None, description="filter by evidence anchor's source_document"),
    significance: str | None = Query(default=None, description="filter by significance value"),
    include_source_only: bool = Query(
        default=False,
        description="when false (default), facts with significance='source_only' are hidden",
    ),
    limit: int = Query(default=100, le=500),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> list[FactDetail]:
    # M02 perimeter (Batch 3): scope every list query to the
    # active person record. Caregivers switching between records
    # see only that record's facts.
    stmt = (
        select(ExtractedFact)
        .where(ExtractedFact.person_record_id == ctx.active_record_id)
        .order_by(ExtractedFact.created_at.desc())
        .limit(limit)
    )
    if review_state:
        stmt = stmt.where(ExtractedFact.review_state == review_state)
    if fact_type:
        stmt = stmt.where(ExtractedFact.fact_type == fact_type)
    if significance:
        stmt = stmt.where(ExtractedFact.significance == significance)
    elif not include_source_only:
        # Q8 (2026-05-11): hide source_only facts by default. The
        # source detail page passes `include_source_only=true` from
        # its "show source-only" toggle.
        stmt = stmt.where(or_(
            ExtractedFact.significance.is_(None),
            ExtractedFact.significance != "source_only",
        ))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(ExtractedFact.label.ilike(pattern), ExtractedFact.description.ilike(pattern)))
    if source_id is not None:
        # Resolve anchor ids for the source, then filter facts whose
        # evidence_anchor_ids array overlaps. Two-step is fine for V1.
        anchor_q = await db.execute(
            select(EvidenceAnchor.id).where(EvidenceAnchor.source_document_id == source_id)
        )
        anchor_ids = list(anchor_q.scalars().all())
        if not anchor_ids:
            return []
        stmt = stmt.where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
    rows = (await db.execute(stmt)).scalars().all()
    sources = await _resolve_source_for_facts(db, list(rows))

    out = []
    for c in rows:
        ua = await _latest_assertion(db, c.id)
        out.append(_detail(c, ua, sources.get(c.id)))
    return out


@router.get("/{fact_id}")
async def get_fact(
    fact_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> FactDetail:
    c = await db.get(ExtractedFact, fact_id)
    # M02 perimeter: 404 on cross-record so we don't disclose
    # existence of another record's fact.
    if c is None or c.person_record_id != ctx.active_record_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ua = await _latest_assertion(db, c.id)
    sources = await _resolve_source_for_facts(db, [c])
    return _detail(c, ua, sources.get(c.id))


# ---------------------------------------------------------------------------
# Fact context (docs/07 R3 — sidesheet view, never the destination)
# ---------------------------------------------------------------------------


class FactContextSource(BaseModel):
    source_id: str | None
    source_name: str | None
    source_type: str | None
    source_page: int | None


class FactContextRelated(BaseModel):
    id: str
    label: str
    display_label: str | None = None
    fact_type: str
    significance: str | None = None
    date_start: datetime | None
    relation: str  # 'same_day_same_source' | 'shared_equivalence_key'


class AlsoRecordedBy(BaseModel):
    """One sibling fact that records the same canonical event (Q7).

    Driven by `equivalence_key`. Surface as "Also recorded by KP
    HealthSummary" — gives the user a quick path to cross-source
    verification without showing duplicate cards everywhere else.
    """

    fact_id: str
    source_id: str | None
    source_name: str | None
    extraction_method: str


class FactContextEpisode(BaseModel):
    """A weak/strong hint that this fact belongs to a larger moment.

    `kind`:
      - `shared_equivalence_key`: same canonical event as N other facts
        (the duplicate-collapsing case — Auto Export + native HK both
        reporting same daily steps).
      - `same_day_same_source`: N other clinical facts on the same UTC
        day from the same source document — strong "they look like
        parts of one surgery / encounter" signal.
      - `none`: no episode hint available (V1 heuristics; R4 will
        replace with HealthEvent canonical groupings).
    """

    kind: str
    title: str
    date_start: datetime | None
    fact_count: int


class FactContextDossier(BaseModel):
    slug: str
    name: str


class FactContextAction(BaseModel):
    kind: str            # confirm | edit | view_source | ask | source_only | open_dossier
    label: str
    href: str | None     # null for in-place actions (e.g. confirm)


class FactContext(BaseModel):
    id: str
    fact_type: str
    label: str
    # docs/07 R5 — patient-readable candidate label; UI prefers this
    # when present, falls back to `label`.
    display_label: str | None = None
    description: str | None
    date_start: datetime | None
    # Section C Phase 1 — drives the date-line copy in the sidesheet
    # (no badge for 'explicit'; "from this visit" for
    # encounter_proximate; "approximate" for issued_approximate;
    # "you confirmed this" for user_canonical; "date unknown" for NULL).
    date_provenance: str | None = None
    historical_status: str | None = None
    review_state: str
    extraction_method: str
    confidence: int | None
    # User-confirmable significance (2026-05-11). The sidesheet shows
    # this prominently and offers "Mark as major/background/source-only"
    # buttons so the user can override.
    significance: str | None = None
    significance_source: str | None = None
    # Plain-language "what this is" — derived deterministically in V1
    # (template by fact_type). R5's LLM relabeling is a separate
    # surface (the display_label field); this remains the
    # interpretation-free "what this is" sentence.
    what_this_is: str
    why_needs_review_text: str | None
    source_context_only_eligible: bool
    source: FactContextSource
    episode: FactContextEpisode | None
    related_facts: list[FactContextRelated]
    # Cross-source siblings (Q7, 2026-05-11). Empty when this fact
    # has no equivalence_key or no peers.
    also_recorded_by: list[AlsoRecordedBy] = []
    matching_dossiers: list[FactContextDossier]
    suggested_actions: list[FactContextAction]


def _what_this_is(c: ExtractedFact) -> str:
    """One-sentence deterministic 'what this is' — never invents
    interpretation. R5 (LLM label translation) will improve this when
    it lands; for V1 we lean on the existing fields and the fact_type
    vocabulary."""
    when = c.date_start.date().isoformat() if c.date_start else None
    label = c.label or "(unlabeled)"
    if c.fact_type == "procedure":
        return f"A procedure recorded as “{label}”" + (f", dated {when}." if when else ".")
    if c.fact_type == "condition":
        return f"A condition recorded as “{label}”" + (f", first noted {when}." if when else ".")
    if c.fact_type == "medication":
        bits = [f"A medication recorded as “{label}”"]
        if c.description:
            bits.append(f"({c.description})")
        if when:
            bits.append(f"on {when}")
        return " ".join(bits).rstrip(".") + "."
    if c.fact_type == "encounter":
        return f"A clinical visit recorded as “{label}”" + (f" on {when}." if when else ".")
    if c.fact_type == "observation":
        return f"A measurement: {label}" + (f" on {when}." if when else ".")
    if c.fact_type == "symptom":
        return f"A symptom recorded as “{label}”" + (f" on {when}." if when else ".")
    if c.fact_type == "lab_result":
        return f"A lab result: {label}" + (f" on {when}." if when else ".")
    if c.fact_type == "provider_relationship":
        return f"A provider / contact recorded as “{label}”."
    return f"{c.fact_type.replace('_', ' ').capitalize()}: {label}" + (f" ({when})." if when else ".")


async def _related_facts(
    db: AsyncSession, c: ExtractedFact
) -> tuple[list[FactContextRelated], FactContextEpisode | None]:
    """Find facts that look like part of the same moment.

    Two heuristics, in priority order:
      1. **Shared equivalence_key**: other facts with the same
         canonical-event key (Auto Export + native HK overlap).
         Strong relation; emits an episode hint.
      2. **Same day + same source**: clinical facts dated on the
         same UTC day, anchored to the same source document. The
         perioperative-bundle case — "this an eye surgery and
         its component procedures are one event." Strong relation.

    R4 will replace these heuristics with HealthEvent rows derived
    at ingest; for V1 we compute on read.
    """
    related: list[FactContextRelated] = []
    episode: FactContextEpisode | None = None

    # Shared equivalence_key path. M02 perimeter (Batch 3):
    # defense-in-depth — scope to the parent fact's record so an
    # equivalence_key collision across records can never surface
    # another patient's fact in this list.
    if c.equivalence_key:
        rows = (await db.execute(
            select(ExtractedFact)
            .where(ExtractedFact.person_record_id == c.person_record_id)
            .where(ExtractedFact.equivalence_key == c.equivalence_key)
            .where(ExtractedFact.id != c.id)
            .where(ExtractedFact.significance != "source_only")
            .limit(20)
        )).scalars().all()
        for r in rows:
            related.append(FactContextRelated(
                id=str(r.id),
                label=r.label,
                display_label=r.display_label,
                fact_type=r.fact_type,
                significance=r.significance,
                date_start=r.date_start,
                relation="shared_equivalence_key",
            ))
        if related:
            episode = FactContextEpisode(
                kind="shared_equivalence_key",
                title=f"Same canonical event ({len(related) + 1} facts across sources)",
                date_start=c.date_start,
                fact_count=len(related) + 1,
            )
            return related, episode

    # Same-day + same-source path. Requires the date to exist.
    if c.date_start is None or not c.evidence_anchor_ids:
        return related, episode

    first_anchor = c.evidence_anchor_ids[0]
    source_id_row = (await db.execute(
        select(EvidenceAnchor.source_document_id)
        .where(EvidenceAnchor.id == first_anchor)
    )).scalar_one_or_none()
    if source_id_row is None:
        return related, episode

    # Pull anchors for that source, find facts on the same UTC day.
    day_start = c.date_start.replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=c.date_start.tzinfo,
    )
    next_day = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    same_source_anchor_ids = list((await db.execute(
        select(EvidenceAnchor.id)
        .where(EvidenceAnchor.source_document_id == source_id_row)
    )).scalars().all())
    if not same_source_anchor_ids:
        return related, episode
    rows = (await db.execute(
        select(ExtractedFact)
        # M02 perimeter (Batch 3): defense-in-depth record scope.
        .where(ExtractedFact.person_record_id == c.person_record_id)
        .where(ExtractedFact.id != c.id)
        .where(ExtractedFact.evidence_anchor_ids.op("&&")(same_source_anchor_ids))
        .where(ExtractedFact.date_start >= day_start)
        .where(ExtractedFact.date_start <= next_day)
        .where(ExtractedFact.fact_type.in_(
            ("procedure", "condition", "encounter", "medication", "observation")
        ))
        # Q8 (2026-05-11): source_only facts are hidden by default
        # in every related list. The source page still shows them
        # via the "Show source-only" toggle.
        .where(ExtractedFact.significance != "source_only")
        .limit(40)
    )).scalars().all()
    # Rank by significance (lowest = most prominent) then date desc.
    from ..canonical.significance import sort_key as _sig_sort_key
    rows = sorted(
        rows,
        key=lambda r: (
            _sig_sort_key(r.significance),
            -(r.date_start.timestamp() if r.date_start else 0),
        ),
    )[:20]
    for r in rows:
        related.append(FactContextRelated(
            id=str(r.id),
            label=r.label,
            display_label=r.display_label,
            fact_type=r.fact_type,
            significance=r.significance,
            date_start=r.date_start,
            relation="same_day_same_source",
        ))
    if related:
        episode = FactContextEpisode(
            kind="same_day_same_source",
            title=f"Same-day events ({len(related) + 1} facts from one source)",
            date_start=c.date_start,
            fact_count=len(related) + 1,
        )

    return related, episode


def _suggested_actions(c: ExtractedFact, source: SourceDocument | None) -> list[FactContextAction]:
    """Action affordances on the sidesheet, ordered by primary intent.

    Always: View source (if available), Ask about this. Conditional:
    Confirm + Edit + Source-only when needs_review. The sidesheet
    renders these as a button row beneath the meaning text."""
    out: list[FactContextAction] = []

    if c.review_state == "needs_review":
        out.append(FactContextAction(kind="confirm", label="Confirm", href=None))
        out.append(FactContextAction(kind="edit", label="Edit details", href=None))
        if c.source_context_only_eligible:
            out.append(FactContextAction(kind="source_only", label="Source-only", href=None))

    if source is not None:
        out.append(FactContextAction(
            kind="view_source",
            label="View source",
            href=f"/sources/{source.id}",
        ))

    out.append(FactContextAction(
        kind="ask",
        label="Ask about this",
        # Pre-fill the Ask page with a question that points at this
        # fact's label. The AskClient now reads ?q= on mount.
        href=f"/ask?q={_ask_question_for_fact(c)}",
    ))

    return out


def _ask_question_for_fact(c: ExtractedFact) -> str:
    """URL-encode-ready question template. Always factual, never
    suggests an interpretation the LLM has to defend."""
    from urllib.parse import quote
    if c.fact_type == "procedure":
        return quote(f"Tell me about the {c.label} procedure on my record.")
    if c.fact_type == "condition":
        return quote(f"What do my records say about {c.label}?")
    if c.fact_type == "medication":
        return quote(f"When and why was {c.label} prescribed?")
    if c.fact_type == "encounter":
        return quote(f"What happened at the {c.label} visit?")
    return quote(f"Tell me more about: {c.label}")


@router.get("/{fact_id}/context")
async def get_fact_context(
    fact_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> FactContext:
    """Patient-meaningful fact-context view (docs/07 R3).

    Replaces the "click fact → land on technical source page"
    pattern. The sidesheet on the frontend opens to this data and
    explains: what this is, why it matters, what other facts are
    related to it, where the evidence lives, and what the user can
    do next.
    """
    c = await db.get(ExtractedFact, fact_id)
    # M02 perimeter: 404 on cross-record. Note _related_facts /
    # the dossier-matching block below all derive scope from `c`
    # which is now provably in-record, so no further filter is
    # load-bearing — but we add explicit person_record_id filters
    # there as defense-in-depth.
    if c is None or c.person_record_id != ctx.active_record_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    sources = await _resolve_source_for_facts(db, [c])
    source = sources.get(c.id)
    related, episode = await _related_facts(db, c)

    # "Also recorded by" siblings (Q7) — only when equivalence_key is
    # set. Resolves each peer's first-anchor source so the UI can show
    # "KP HealthSummary" etc. M02 perimeter: scope peers to the active
    # record so an equivalence_key collision cannot leak peers.
    also_recorded_by: list[AlsoRecordedBy] = []
    if c.equivalence_key:
        peers = list((await db.execute(
            select(ExtractedFact)
            .where(ExtractedFact.person_record_id == ctx.active_record_id)
            .where(ExtractedFact.equivalence_key == c.equivalence_key)
            .where(ExtractedFact.id != c.id)
            .limit(8)
        )).scalars().all())
        peer_sources = await _resolve_source_for_facts(db, peers)
        for p in peers:
            s = peer_sources.get(p.id)
            also_recorded_by.append(AlsoRecordedBy(
                fact_id=str(p.id),
                source_id=str(s.id) if s else None,
                source_name=(s.source_label or s.original_filename) if s else None,
                extraction_method=p.extraction_method,
            ))

    # Dossier linkages — same predicate as elsewhere, scored against
    # the active record's topics. Bounded query — most installs have
    # <20 topics per record. M02 perimeter: post-migration 0032
    # Topics carry person_record_id; scope explicitly.
    from ..models.topic import Topic
    from ..retrieval.topics import topic_membership_clause
    topics = list((await db.execute(
        select(Topic).where(Topic.person_record_id == ctx.active_record_id)
    )).scalars().all())
    matching: list[FactContextDossier] = []
    for t in topics:
        clause = topic_membership_clause(t)
        if clause is None:
            continue
        hit = (await db.execute(
            select(ExtractedFact.id)
            .where(ExtractedFact.id == c.id)
            .where(clause)
        )).scalar_one_or_none()
        if hit is not None:
            matching.append(FactContextDossier(slug=t.slug, name=t.name))

    # Page number (PDF anchor) if available — surfaces "view source
    # at page N" deep links on the action row.
    page_number: int | None = None
    if c.evidence_anchor_ids:
        anchor = await db.get(EvidenceAnchor, c.evidence_anchor_ids[0])
        if anchor is not None:
            page_number = anchor.page_number

    # Section C Phase 1 — surface user_canonical provenance when the
    # user has overridden the date via UserAssertion. Single bonus
    # query; small + cached by SQLAlchemy identity map if /detail
    # was hit on the same fact.
    ua_for_prov = await _latest_assertion(db, c.id)
    effective_prov = (
        "user_canonical"
        if (ua_for_prov and ua_for_prov.canonical_date_start)
        else c.date_provenance
    )

    return FactContext(
        id=str(c.id),
        fact_type=c.fact_type,
        label=c.label,
        display_label=c.display_label,
        description=c.description,
        date_start=c.date_start,
        date_provenance=effective_prov,
        historical_status=c.historical_status,
        review_state=c.review_state,
        extraction_method=c.extraction_method,
        confidence=c.confidence,
        significance=c.significance,
        significance_source=c.significance_source,
        what_this_is=_what_this_is(c),
        why_needs_review_text=c.why_needs_review_text,
        source_context_only_eligible=c.source_context_only_eligible,
        source=FactContextSource(
            source_id=str(source.id) if source else None,
            source_name=(source.source_label or source.original_filename) if source else None,
            source_type=source.source_type if source else None,
            source_page=page_number,
        ),
        episode=episode,
        related_facts=related,
        also_recorded_by=also_recorded_by,
        matching_dossiers=matching,
        suggested_actions=_suggested_actions(c, source),
    )


@router.patch("/{fact_id}")
async def correct_fact(
    fact_id: uuid.UUID,
    body: CorrectionRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> FactDetail:
    user = ctx.user
    c = await db.get(ExtractedFact, fact_id)
    # M02 perimeter: 404 on cross-record so a caregiver cannot
    # confirm/correct facts on a record they don't have access to.
    if c is None or c.person_record_id != ctx.active_record_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if body.assertion_type not in {"confirm", "correct", "reject", "annotate"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assertion_type")

    ua = UserAssertion(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        related_fact_id=c.id,
        assertion_type=body.assertion_type,
        canonical_label=body.canonical_label,
        canonical_description=body.canonical_description,
        canonical_date_start=body.canonical_date_start,
        canonical_date_end=body.canonical_date_end,
        reason=body.reason,
    )
    db.add(ua)

    # Bump the review state on the original fact. We do NOT mutate any
    # other field — original extraction is preserved.
    state_map = {
        "confirm": "confirmed",
        "correct": "corrected",
        "reject": "rejected",
        "annotate": c.review_state,
    }
    next_state = body.new_review_state or state_map.get(body.assertion_type, c.review_state)
    if next_state != c.review_state:
        c.review_state = next_state

    await db.commit()
    await db.refresh(c)
    sources = await _resolve_source_for_facts(db, [c])
    return _detail(c, ua, sources.get(c.id))


# ---------------------------------------------------------------------------
# Bulk review-inbox actions (#54) — defer/reject/confirm many facts at once
# ---------------------------------------------------------------------------


class BulkRequest(BaseModel):
    fact_ids: list[uuid.UUID]
    # Same vocabulary as the single-fact path — we map to review_state
    # the same way. "annotate" + new_review_state lets the caller bulk-
    # set arbitrary states (e.g. "deferred") without claiming a stronger
    # assertion type.
    assertion_type: str
    new_review_state: str | None = None
    reason: str | None = None


class BulkResult(BaseModel):
    updated: int
    not_found: list[str]
    failed: list[str]
    review_state: str | None  # the state every successfully-updated fact ended in


class RelabelBackfillResult(BaseModel):
    checked: int
    relabeled: int
    declined: int
    errored: int


@router.post("/relabel-backfill")
async def relabel_backfill(
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=200),
) -> RelabelBackfillResult:
    """Run the R5 candidate-display-label backfill for up to `limit`
    facts that don't yet have a display_label.

    Manually-triggered for V1 — caller controls cost ceiling by
    setting `limit`. Each successful relabel writes a ModelRun audit
    row via call_with_tool so spend is observable.

    PHI consent must be granted; the call sends fact labels +
    optional descriptions to Anthropic.

    M02 perimeter: scope is the active record. Caregivers running
    relabel against record A do not touch record B's facts even
    while iterating the global table.
    """
    from ..core.consent import require_phi_consent
    from ..llm.relabel import relabel_pending
    user = ctx.user
    require_phi_consent(user)
    summary = await relabel_pending(
        db, user, limit=limit, person_record_id=ctx.active_record_id,
    )
    return RelabelBackfillResult(**summary)


class SignificanceBackfillResult(BaseModel):
    checked: int
    assigned: int
    skipped: int  # rows that already had user/llm-set significance


@router.post("/significance-backfill")
async def significance_backfill(
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=2000, ge=1, le=20000),
) -> SignificanceBackfillResult:
    """Apply the deterministic significance heuristic to every fact
    that doesn't yet have one set.

    Idempotent: rows with `significance_source ∈ {user, llm}` are
    skipped (those overrides win). Rows with `significance_source =
    'heuristic'` are recomputed when the heuristic is improved — to
    force a recompute pass `?force=true`. Plain re-runs only touch
    NULL or `default` rows.

    M02 perimeter: scoped to the active person_record so a
    caregiver's backfill only touches that record's rows.
    """
    from datetime import datetime, timezone as _tz
    from ..canonical.significance import compute as _compute

    rows = list((await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.person_record_id == ctx.active_record_id)
        .where(or_(
            ExtractedFact.significance.is_(None),
            ExtractedFact.significance_source == "default",
        ))
        .order_by(ExtractedFact.created_at.desc())
        .limit(limit)
    )).scalars().all())
    assigned = 0
    skipped = 0
    now = datetime.now(_tz.utc)
    for r in rows:
        if r.significance_source in {"user", "llm"}:
            skipped += 1
            continue
        r.significance = _compute(r)
        r.significance_source = "heuristic"
        r.significance_set_at = now
        assigned += 1
    if assigned:
        await db.commit()
    return SignificanceBackfillResult(
        checked=len(rows), assigned=assigned, skipped=skipped,
    )


class SignificanceRequest(BaseModel):
    significance: str
    reason: str | None = None


class SignificanceResponse(BaseModel):
    id: str
    significance: str
    significance_source: str


@router.patch("/{fact_id}/significance", response_model=SignificanceResponse)
async def set_fact_significance(
    fact_id: uuid.UUID,
    body: SignificanceRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> SignificanceResponse:
    """User override of a fact's significance. Always wins over the
    heuristic or LLM. Audited via `audit_events` so the user can ask
    later why a fact is or isn't surfacing."""
    from datetime import datetime, timezone as _tz
    from ..canonical.significance import RANK
    from ..models.audit_event import AuditEvent

    user = ctx.user
    if body.significance not in RANK:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"significance must be one of {sorted(RANK.keys())}",
        )
    c = await db.get(ExtractedFact, fact_id)
    # M02 perimeter: 404 on cross-record.
    if c is None or c.person_record_id != ctx.active_record_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    prev = c.significance
    c.significance = body.significance
    c.significance_source = "user"
    c.significance_set_at = datetime.now(_tz.utc)

    db.add(AuditEvent(
        user_id=user.id,
        event_type="significance_change",
        subject_type="fact",
        subject_id=str(fact_id),
        detail={
            "from": prev,
            "to": body.significance,
            "reason": body.reason,
        },
    ))
    await db.commit()
    return SignificanceResponse(
        id=str(c.id),
        significance=c.significance,
        significance_source=c.significance_source,
    )


@router.post("/bulk")
async def bulk_correct_facts(
    body: BulkRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> BulkResult:
    """Apply the same review action to many facts in one transaction.

    Powers the review-inbox lane bulk actions: "Defer all 23 from this
    source", "Reject selected", etc. Every fact gets its own
    UserAssertion row so the audit trail records each disposition.
    """
    user = ctx.user
    if body.assertion_type not in {"confirm", "correct", "reject", "annotate"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assertion_type"
        )
    if not body.fact_ids:
        return BulkResult(updated=0, not_found=[], failed=[], review_state=None)

    state_map = {
        "confirm": "confirmed",
        "correct": "corrected",
        "reject": "rejected",
        "annotate": None,  # use existing state unless new_review_state set
    }
    target_state = body.new_review_state or state_map.get(body.assertion_type)

    # M02 perimeter: scope the SELECT to the active record so a
    # mixed list of in-record + cross-record ids only mutates the
    # in-record subset. Cross-record ids land in `not_found` —
    # indistinguishable from "id doesn't exist," which is correct
    # behavior (don't disclose existence outside the record).
    rows = (await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.person_record_id == ctx.active_record_id)
        .where(ExtractedFact.id.in_(body.fact_ids))
    )).scalars().all()
    found_ids = {r.id for r in rows}
    not_found = [str(fid) for fid in body.fact_ids if fid not in found_ids]

    updated = 0
    failed: list[str] = []
    for c in rows:
        try:
            db.add(UserAssertion(
                user_id=user.id,
                person_record_id=ctx.active_record_id,
                related_fact_id=c.id,
                assertion_type=body.assertion_type,
                reason=body.reason,
            ))
            if target_state is not None and target_state != c.review_state:
                c.review_state = target_state
            updated += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{c.id}: {e}")
    await db.commit()

    return BulkResult(
        updated=updated,
        not_found=not_found,
        failed=failed,
        review_state=target_state,
    )
