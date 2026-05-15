"""Topic + dossier endpoints."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.consent import require_phi_consent
from ..core.db import get_session
from ..llm import call_with_tool, get_registry
from ..models.brief_message import BriefMessage
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.topic import Topic
from ..models.topic_brief import TopicBrief
from ..models.user import User
from ..models.user_assertion import UserAssertion
from ..retrieval.topics import (
    facts_for_topic,
    hidden_review_states,
    search_facts,
    topic_membership_clause,
)
from .auth import get_current_user

# Self-harm guardrail keywords. Same approach as routes/ask.py — best-effort
# input filter; the LLM's own refusal also kicks in via safety_response.
_SELF_HARM_PATTERNS = re.compile(
    r"\b("
    r"kill\s+(myself|me)|suicid|end\s+my\s+life|"
    r"hurt\s+myself|self[-\s]?harm|cut\s+myself"
    r")\b",
    re.IGNORECASE,
)
_SELF_HARM_RESPONSE = (
    "If you're in crisis, please reach a person right now. In the US, "
    "call or text 988 for the Suicide & Crisis Lifeline. Outside the US, "
    "the IASP directory at https://www.iasp.info/resources/Crisis_Centres/ "
    "lists local options. I'm not able to help you with this through your "
    "record — but a human can."
)

router = APIRouter()


class TopicSummary(BaseModel):
    id: str
    name: str
    slug: str
    aliases: list[str]
    description: str | None


class CreateTopic(BaseModel):
    name: str
    aliases: list[str] = []
    description: str | None = None


class FactReadout(BaseModel):
    id: str
    fact_type: str
    label: str
    # docs/07 R5 — UI prefers display_label when present.
    display_label: str | None = None
    description: str | None
    date_start: datetime | None
    date_end: datetime | None
    date_precision: str | None
    confidence: int | None
    review_state: str
    extraction_method: str
    body_site: str | None
    laterality: str | None
    canonical_label: str | None
    canonical_description: str | None
    canonical_date_start: datetime | None
    # Source-link: enough to deep-link a fact back to its source page or
    # photo. Drawn from the first evidence anchor (the canonical one for
    # vision-extracted facts; arbitrary but stable for CCDA/notes).
    source_id: str | None = None
    source_page: int | None = None
    source_anchor_id: str | None = None
    source_anchor_type: str | None = None
    # The supporting text excerpt the extractor wrote into the anchor —
    # the actual "why do you think that?" evidence quoted from the
    # source. Limited to 280 chars in the readout to keep the payload
    # bounded; the full text is available on the source detail page.
    source_anchor_excerpt: str | None = None
    source_anchor_section_path: str | None = None


class FactCluster(BaseModel):
    """A collapsed group of similar facts in a dossier.

    Per docs/06: "Browsing should favor clusters over rows, stories
    over tables, evidence on demand." Each cluster summarizes a group
    of facts that share a label (e.g., 30k "Heart rate: ..." readings
    collapse into one cluster). The dossier renders one card per
    cluster with date range + counts; the user expands to see the
    individual facts.
    """

    cluster_id: str  # deterministic; safe to use in URLs
    fact_type: str
    label: str  # representative
    date_start_min: datetime | None
    date_start_max: datetime | None
    fact_count: int
    source_count: int
    needs_review_count: int


class DossierResponse(BaseModel):
    topic: TopicSummary
    clusters: list[FactCluster]
    total_facts: int
    # A small sample of dated facts used to render the dossier
    # timeline. Full per-cluster fact lists are fetched on demand via
    # /api/topics/{slug}/clusters/{cluster_id}/facts.
    timeline_facts: list[FactReadout]


class ExecBriefResponse(BaseModel):
    topic_slug: str
    brief_id: str | None
    model_run_id: str | None
    prompt_version: str | None
    generated_at: datetime | None
    error: str | None
    narrative: str | None
    well_supported: list[dict]
    uncertain: list[dict]
    suggested_questions: list[str]
    citations: list[dict]
    safety_response: str | None


def _brief_response(topic_slug: str, brief: TopicBrief | None) -> ExecBriefResponse | None:
    if brief is None:
        return None
    return ExecBriefResponse(
        topic_slug=topic_slug,
        brief_id=str(brief.id),
        model_run_id=str(brief.model_run_id) if brief.model_run_id else None,
        prompt_version=brief.prompt_version,
        generated_at=brief.generated_at,
        error=brief.error,
        narrative=brief.narrative,
        well_supported=list(brief.well_supported or []),
        uncertain=list(brief.uncertain or []),
        suggested_questions=list(brief.suggested_questions or []),
        citations=list(brief.citations or []),
        safety_response=brief.safety_response,
    )


def _slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-") or "topic"


def _topic_summary(t: Topic) -> TopicSummary:
    return TopicSummary(
        id=str(t.id),
        name=t.name,
        slug=t.slug,
        aliases=list(t.aliases or []),
        description=t.description,
    )


async def _fact_readouts(db: AsyncSession, facts: list[ExtractedFact]) -> list[FactReadout]:
    if not facts:
        return []
    fact_ids = [c.id for c in facts]
    canon_q = await db.execute(
        select(UserAssertion).where(UserAssertion.related_fact_id.in_(fact_ids))
    )
    by_fact: dict[uuid.UUID, UserAssertion] = {}
    for a in canon_q.scalars().all():
        if a.related_fact_id is not None:
            by_fact[a.related_fact_id] = a

    # Batch-load the first evidence anchor for each fact to expose source/page links.
    all_anchor_ids: list[uuid.UUID] = []
    first_anchor_for: dict[uuid.UUID, uuid.UUID] = {}
    for c in facts:
        if c.evidence_anchor_ids:
            first = c.evidence_anchor_ids[0]
            first_anchor_for[c.id] = first
            all_anchor_ids.append(first)
    anchors_by_id: dict[uuid.UUID, EvidenceAnchor] = {}
    if all_anchor_ids:
        anc_q = await db.execute(select(EvidenceAnchor).where(EvidenceAnchor.id.in_(all_anchor_ids)))
        for a in anc_q.scalars().all():
            anchors_by_id[a.id] = a

    out = []
    for c in facts:
        canon = by_fact.get(c.id)
        anchor = anchors_by_id.get(first_anchor_for.get(c.id, uuid.UUID(int=0)))
        out.append(
            FactReadout(
                id=str(c.id),
                fact_type=c.fact_type,
                label=c.label,
                display_label=c.display_label,
                description=c.description,
                date_start=c.date_start,
                date_end=c.date_end,
                date_precision=c.date_precision,
                confidence=c.confidence,
                review_state=c.review_state,
                extraction_method=c.extraction_method,
                body_site=c.body_site,
                laterality=c.laterality,
                canonical_label=canon.canonical_label if canon else None,
                canonical_description=canon.canonical_description if canon else None,
                canonical_date_start=canon.canonical_date_start if canon else None,
                source_id=str(anchor.source_document_id) if anchor else None,
                source_page=anchor.page_number if anchor else None,
                source_anchor_id=str(anchor.id) if anchor else None,
                source_anchor_type=anchor.anchor_type if anchor else None,
                source_anchor_excerpt=(
                    (anchor.text_excerpt or "")[:280] or None
                ) if anchor else None,
                source_anchor_section_path=anchor.section_path if anchor else None,
            )
        )
    return out


@router.get("")
async def list_topics(
    q: str | None = Query(default=None, description="Match name, slug, or any alias (ILIKE)."),
    limit: int = Query(default=200, ge=1, le=500),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[TopicSummary]:
    """List Dossiers. `q` powers the chat Save menu's attach picker."""
    from sqlalchemy import text as _text
    stmt = select(Topic).order_by(Topic.name).limit(limit)
    if q:
        pat = f"%{q.strip()}%"
        alias_match = _text(
            "EXISTS (SELECT 1 FROM unnest(topics.aliases) a WHERE a ILIKE :pat)"
        ).bindparams(pat=pat)
        stmt = stmt.where(or_(
            func.lower(Topic.name).like(func.lower(pat)),
            func.lower(Topic.slug).like(func.lower(pat)),
            alias_match,
        ))
    result = await db.execute(stmt)
    return [_topic_summary(t) for t in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_topic(
    body: CreateTopic,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> TopicSummary:
    slug = _slugify(body.name)
    existing = await db.execute(select(Topic).where(Topic.slug == slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Topic '{slug}' already exists",
        )
    t = Topic(
        name=body.name,
        slug=slug,
        aliases=body.aliases,
        description=body.description,
        related_concepts=[],
        created_by=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _topic_summary(t)


async def _resolve_topic_or_404(db: AsyncSession, slug: str) -> Topic:
    result = await db.execute(select(Topic).where(Topic.slug == slug))
    t = result.scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic not found")
    return t


def _cluster_id_for(fact_type: str, normalized_label: str) -> str:
    """Deterministic 12-char id for `(fact_type, normalized_label)`.

    Used in URLs (`/clusters/{cluster_id}/facts`). The endpoint
    enumerates a topic's grouping keys to find the matching pair —
    no persisted cluster rows, cheap and stale-proof.

    The grouping rule lives in the dossier route's ``norm_expr`` SQL
    expression: ``lower(trim(regexp_replace(split_part(label, ':', 1), '\\s+', ' ', 'g')))``.
    Auto Export labels (``Heart rate: 72 bpm``) collapse on the prefix
    before ':'; CCDA / vision labels (no colon) match on the whole
    label whitespace-normalized.
    """
    raw = f"{fact_type}|{normalized_label}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _clean_cluster_header(rep_label: str) -> str:
    """Display label for a cluster card.

    For Auto Export-style ``Metric: value`` labels, drop the value to
    show ``Heart rate`` rather than ``Heart rate: 72 bpm``. For other
    labels, return as-is.
    """
    if ":" in rep_label:
        return rep_label.split(":", 1)[0].strip()
    return rep_label


@router.get("/{slug}")
async def get_topic_dossier(
    slug: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> DossierResponse:
    """Return the dossier as cluster summaries + a small timeline sample.

    Per docs/06: clusters over rows, evidence on demand. The cluster
    cards collapse repetitive facts ("Heart rate: …" × 30k); the
    timeline sample gives the dossier shape without dumping every
    fact into the response. Per-cluster expansion happens via
    `/clusters/{cluster_id}/facts`.
    """
    topic = await _resolve_topic_or_404(db, slug)
    clause = topic_membership_clause(topic)
    if clause is None:
        return DossierResponse(
            topic=_topic_summary(topic),
            clusters=[],
            total_facts=0,
            timeline_facts=[],
        )

    # Aggregation key the SQL group-by uses — same shape as
    # `_normalize_cluster_label` but expressed in Postgres so we never
    # hydrate 30k+ ORM rows just to count.
    norm_expr = func.lower(
        func.trim(
            func.regexp_replace(
                func.split_part(ExtractedFact.label, ":", 1), r"\s+", " ", "g"
            )
        )
    )

    base_filter = ExtractedFact.review_state.notin_(hidden_review_states())

    # Query 1: cluster aggregates (counts, date range, representative
    # label) — entirely in SQL.
    cluster_q = await db.execute(
        select(
            ExtractedFact.fact_type,
            norm_expr.label("norm_label"),
            func.count().label("fact_count"),
            func.count().filter(ExtractedFact.review_state == "needs_review").label("needs_review_count"),
            func.min(ExtractedFact.date_start).label("dmin"),
            func.max(ExtractedFact.date_start).label("dmax"),
            # Representative label = the longest non-null one in the group.
            (
                func.array_agg(
                    ExtractedFact.label,
                    order_by=func.length(ExtractedFact.label).desc().nullslast(),
                )
            ).label("labels_by_length"),
        )
        .where(clause)
        .where(base_filter)
        .group_by(ExtractedFact.fact_type, norm_expr)
    )
    rows = cluster_q.all()

    total_facts = sum(int(r.fact_count) for r in rows)

    # Query 2: distinct source counts per cluster — done by joining
    # extracted_facts → unnest(evidence_anchor_ids) → evidence_anchors
    # and grouping on the same key.
    anchor_id = func.unnest(ExtractedFact.evidence_anchor_ids).label("anchor_id")
    sub = (
        select(
            ExtractedFact.fact_type.label("ft"),
            norm_expr.label("nl"),
            anchor_id,
        )
        .where(clause)
        .where(base_filter)
        .subquery()
    )
    src_q = await db.execute(
        select(
            sub.c.ft,
            sub.c.nl,
            func.count(func.distinct(EvidenceAnchor.source_document_id)).label("src_count"),
        )
        .join(EvidenceAnchor, EvidenceAnchor.id == sub.c.anchor_id)
        .group_by(sub.c.ft, sub.c.nl)
    )
    src_by_key: dict[tuple[str, str], int] = {
        (ft, nl): int(c) for (ft, nl, c) in src_q.all()
    }

    clusters_out: list[FactCluster] = []
    for r in rows:
        rep = ""
        if r.labels_by_length:
            for candidate in r.labels_by_length:
                if candidate:
                    rep = candidate
                    break
        clusters_out.append(
            FactCluster(
                cluster_id=_cluster_id_for(r.fact_type, r.norm_label or ""),
                fact_type=r.fact_type,
                label=_clean_cluster_header(rep) or rep or (r.norm_label or ""),
                date_start_min=r.dmin,
                date_start_max=r.dmax,
                fact_count=int(r.fact_count),
                source_count=src_by_key.get((r.fact_type, r.norm_label or ""), 0),
                needs_review_count=int(r.needs_review_count),
            )
        )

    # Sort: needs-review first (surface what wants attention), then
    # fact_count desc, then latest date desc as a tiebreak.
    clusters_out.sort(
        key=lambda c: (
            -c.needs_review_count,
            -c.fact_count,
            -(c.date_start_max.timestamp() if c.date_start_max else 0),
            c.label.lower(),
        )
    )

    # Query 3: a small timeline sample — top dated facts ordered by
    # date. We don't need 5-per-cluster anymore now that the cards
    # show the date range; a flat top-N is simpler and the timeline's
    # role is to give the dossier its shape, not to enumerate.
    tl_q = await db.execute(
        select(ExtractedFact)
        .where(clause)
        .where(base_filter)
        .where(ExtractedFact.date_start.isnot(None))
        .order_by(ExtractedFact.date_start.asc())
        .limit(200)
    )
    timeline_facts = list(tl_q.scalars().all())

    return DossierResponse(
        topic=_topic_summary(topic),
        clusters=clusters_out,
        total_facts=total_facts,
        timeline_facts=await _fact_readouts(db, timeline_facts),
    )


@router.get("/{slug}/clusters/{cluster_id}/facts")
async def get_cluster_facts(
    slug: str,
    cluster_id: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    limit: int = 500,
) -> list[FactReadout]:
    """Return the facts inside one cluster, newest first.

    Capped at `limit` (default 500) — for Auto Export clusters with
    30k+ facts we don't try to ship all of them; the user gets the
    most recent slice and can drill deeper via Discover or the
    per-metric layer (#46) once that lands.
    """
    topic = await _resolve_topic_or_404(db, slug)
    clause = topic_membership_clause(topic)
    if clause is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    base_filter = ExtractedFact.review_state.notin_(hidden_review_states())
    norm_expr = func.lower(
        func.trim(
            func.regexp_replace(
                func.split_part(ExtractedFact.label, ":", 1), r"\s+", " ", "g"
            )
        )
    )

    # Resolve which (fact_type, normalized_label) pair the cluster_id
    # encodes by enumerating the topic's distinct grouping keys.
    key_q = await db.execute(
        select(ExtractedFact.fact_type, norm_expr.label("norm"))
        .where(clause)
        .where(base_filter)
        .group_by(ExtractedFact.fact_type, norm_expr)
    )
    match: tuple[str, str] | None = None
    for ft, nl in key_q.all():
        if _cluster_id_for(ft, nl or "") == cluster_id:
            match = (ft, nl or "")
            break
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")

    cap = max(1, min(limit, 2000))
    fact_q = await db.execute(
        select(ExtractedFact)
        .where(clause)
        .where(base_filter)
        # Q8 (2026-05-11): source_only facts are hidden by default in
        # the cluster list. They're still searchable from the source
        # detail page's "show source-only" toggle.
        .where(or_(
            ExtractedFact.significance.is_(None),
            ExtractedFact.significance != "source_only",
        ))
        .where(ExtractedFact.fact_type == match[0])
        .where(norm_expr == match[1])
        .order_by(
            ExtractedFact.date_start.desc().nullslast(),
            ExtractedFact.created_at.desc(),
        )
        .limit(cap)
    )
    facts = list(fact_q.scalars().all())
    return await _fact_readouts(db, facts)


def _format_facts_block(facts: list[ExtractedFact]) -> str:
    lines = []
    for c in facts:
        date_str = ""
        if c.date_start:
            date_str = c.date_start.date().isoformat()
            if c.date_precision and c.date_precision != "day":
                date_str = f"{date_str} ({c.date_precision})"
        body = c.description or c.label
        lines.append(
            f"- fact_id={c.id} type={c.fact_type} date={date_str or '?'} confidence={c.confidence or '?'}\n"
            f"  label: {c.label}\n  excerpt: {body[:300]}"
        )
    return "\n".join(lines) if lines else "(no facts yet)"


async def _latest_brief(db: AsyncSession, topic_id: uuid.UUID) -> TopicBrief | None:
    return (await db.execute(
        select(TopicBrief)
        .where(TopicBrief.topic_id == topic_id)
        .order_by(TopicBrief.generated_at.desc())
        .limit(1)
    )).scalar_one_or_none()


@router.get("/{slug}/brief")
async def get_latest_brief(
    slug: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ExecBriefResponse | None:
    """Return the latest persisted brief for this topic, or null if none.

    Cheap read — no Anthropic call. The dossier hydrates this on render
    so users don't pay for regeneration just to view what's already
    been written.
    """
    topic = await _resolve_topic_or_404(db, slug)
    brief = await _latest_brief(db, topic.id)
    return _brief_response(topic.slug, brief)


@router.post("/{slug}/brief")
async def generate_exec_brief(
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ExecBriefResponse:
    """Generate a fresh brief and persist it as a TopicBrief row.

    Always writes a new row (versioned, not regenerate-in-place) so
    users can diff understanding over time as new evidence lands.
    """
    require_phi_consent(user)
    topic = await _resolve_topic_or_404(db, slug)
    facts = await facts_for_topic(db, topic)

    prompt = get_registry().get("dossier_brief")
    user_vars = {
        "topic_name": topic.name,
        "topic_aliases": ", ".join(topic.aliases or []) or "(none)",
        "facts_block": _format_facts_block(facts),
        "assertions_block": "(canonical assertions are merged into facts at retrieval; nothing extra here for V1)",
    }
    result = await call_with_tool(
        db, user, prompt,
        user_vars=user_vars,
        purpose="dossier_brief",
        input_source_ids=[],
        tool_name="emit_brief",
    )
    out = result.tool_input or {}

    brief = TopicBrief(
        topic_id=topic.id,
        model_run_id=result.model_run_id,
        prompt_version=prompt.version_tag,
        narrative=out.get("narrative"),
        well_supported=out.get("well_supported", []) or [],
        uncertain=out.get("uncertain", []) or [],
        suggested_questions=out.get("suggested_questions", []) or [],
        citations=out.get("citations", []) or [],
        safety_response=out.get("safety_response"),
        error=result.error,
    )
    db.add(brief)
    await db.commit()
    await db.refresh(brief)

    return _brief_response(topic.slug, brief)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Threaded follow-up on a dossier brief
# ---------------------------------------------------------------------------


class BriefMessageReadout(BaseModel):
    id: str
    role: str  # 'user' | 'assistant'
    content: str
    citations: list[dict]
    retrieved_fact_count: int | None
    model_run_id: str | None
    safety_response: str | None
    error: str | None
    created_at: datetime


def _msg_readout(m: BriefMessage) -> BriefMessageReadout:
    return BriefMessageReadout(
        id=str(m.id),
        role=m.role,
        content=m.content,
        citations=list(m.citations or []),
        retrieved_fact_count=m.retrieved_fact_count,
        model_run_id=str(m.model_run_id) if m.model_run_id else None,
        safety_response=m.safety_response,
        error=m.error,
        created_at=m.created_at,
    )


class FollowupRequest(BaseModel):
    question: str


def _is_self_harm(text: str) -> bool:
    return bool(_SELF_HARM_PATTERNS.search(text or ""))


def _format_brief_for_prompt(brief: TopicBrief | None) -> str:
    if brief is None or not brief.narrative:
        return "(no brief has been generated yet for this topic)"
    well = brief.well_supported or []
    unc = brief.uncertain or []
    parts = [brief.narrative.strip()]
    if well:
        parts.append("Well-supported (from the brief):")
        for w in well:
            parts.append(f"  - {w.get('statement', '')} [fact_ids: {', '.join(w.get('fact_ids', []) or [])}]")
    if unc:
        parts.append("Uncertain (from the brief):")
        for u in unc:
            why = u.get("why_uncertain")
            line = f"  - {u.get('statement', '')}"
            if why:
                line += f" — {why}"
            line += f" [fact_ids: {', '.join(u.get('fact_ids', []) or [])}]"
            parts.append(line)
    return "\n".join(parts)


def _format_messages_for_prompt(msgs: list[BriefMessage], limit: int = 12) -> str:
    """Render the last N turns as plain dialogue for the prompt context.

    We cap at `limit` turns and drop earlier history to keep the prompt
    bounded — V1 conversations are short; long-context summarization is
    a V1.1 problem.
    """
    if not msgs:
        return "(this is the first follow-up; no prior turns)"
    tail = msgs[-limit:]
    lines = []
    for m in tail:
        prefix = "USER:" if m.role == "user" else "ASSISTANT:"
        lines.append(f"{prefix} {m.content}")
    return "\n".join(lines)


async def _thread_for_topic(db: AsyncSession, topic_id: uuid.UUID) -> list[BriefMessage]:
    res = await db.execute(
        select(BriefMessage)
        .where(BriefMessage.topic_id == topic_id)
        .order_by(BriefMessage.created_at.asc())
    )
    return list(res.scalars().all())


@router.get("/{slug}/thread")
async def get_brief_thread(
    slug: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[BriefMessageReadout]:
    """Return the conversation thread for this topic's dossier brief.

    Legacy path retained for backward compatibility. New clients
    should use POST /api/topics/{slug}/conversation to get a
    docs/10 Conversation object scoped to this topic, then
    interact through /api/conversations/{id}/messages.
    """
    topic = await _resolve_topic_or_404(db, slug)
    msgs = await _thread_for_topic(db, topic.id)
    return [_msg_readout(m) for m in msgs]


class TopicConversationOut(BaseModel):
    conversation_id: str
    created: bool


class DossierConversationOut(BaseModel):
    id: str
    title: str | None
    kind: str
    last_message_at: datetime | None
    created_at: datetime
    starred: bool
    archived: bool


@router.get("/{slug}/conversations", response_model=list[DossierConversationOut])
async def list_topic_conversations(
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[DossierConversationOut]:
    """List conversations that have been promoted into this dossier.

    Includes conversations promoted via `POST /api/conversations/{id}/
    save-as-topic` and any dossier_followup threads created via
    `POST /api/topics/{slug}/conversation`. Newest-active first.
    """
    from sqlalchemy import text as _text
    from ..models.conversation import Conversation
    topic = await _resolve_topic_or_404(db, slug)
    rows = (await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .where(Conversation.archived.is_(False))
        .where(_text("scope->>'topic_slug' = :slug").bindparams(slug=topic.slug))
        .order_by(
            Conversation.last_message_at.desc().nullslast(),
            Conversation.created_at.desc(),
        )
    )).scalars().all()
    return [
        DossierConversationOut(
            id=str(r.id),
            title=r.title,
            kind=r.kind,
            last_message_at=r.last_message_at,
            created_at=r.created_at,
            starred=r.starred,
            archived=r.archived,
        )
        for r in rows
    ]


@router.post("/{slug}/conversation", response_model=TopicConversationOut,
             status_code=status.HTTP_201_CREATED)
async def get_or_create_topic_conversation(
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> TopicConversationOut:
    """Get-or-create the user's dossier-scoped Conversation for this
    topic (iOS-asked migration target away from `/brief`).

    Reuses the most recent non-archived Conversation whose
    scope.topic_slug matches; otherwise creates a fresh one with
    kind='dossier_followup' so subsequent /api/conversations/{id}
    /messages calls land in a stable thread.
    """
    from ..llm.conversations import create_conversation
    from ..models.conversation import Conversation
    topic = await _resolve_topic_or_404(db, slug)

    # Manual scope.topic_slug lookup — JSONB containment query.
    from sqlalchemy import text as _text
    row = (await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .where(Conversation.kind == "dossier_followup")
        .where(Conversation.archived.is_(False))
        .where(_text("scope->>'topic_slug' = :slug").bindparams(slug=topic.slug))
        .order_by(Conversation.last_message_at.desc().nullslast(),
                  Conversation.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is not None:
        return TopicConversationOut(conversation_id=str(row.id), created=False)

    conv = await create_conversation(
        db, user,
        kind="dossier_followup",
        title=f"Dossier: {topic.name}",
        scope={"type": "topic", "topic_slug": topic.slug},
    )
    return TopicConversationOut(conversation_id=str(conv.id), created=True)


class AttachConversationToTopicRequest(BaseModel):
    conversation_id: uuid.UUID


class AttachConversationToTopicResponse(BaseModel):
    slug: str
    conversation_id: str
    already_attached: bool


@router.post("/{slug}/attach-conversation",
             response_model=AttachConversationToTopicResponse)
async def attach_conversation_to_topic_route(
    slug: str,
    body: AttachConversationToTopicRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AttachConversationToTopicResponse:
    """Attach an existing Conversation to an existing Dossier.

    Re-scopes the conversation so retrieval picks up the dossier's
    membership clause on future messages. Idempotent — if the
    conversation already points at this slug, returns
    already_attached=true without changing anything else.
    """
    from ..models.conversation import Conversation

    topic = await _resolve_topic_or_404(db, slug)
    conv = await db.get(Conversation, body.conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    current_scope = conv.scope or {}
    already = (
        isinstance(current_scope, dict)
        and current_scope.get("type") == "topic"
        and current_scope.get("topic_slug") == topic.slug
    )
    if not already:
        conv.scope = {"type": "topic", "topic_slug": topic.slug}
        conv.kind = "dossier_followup"
        await db.commit()
    return AttachConversationToTopicResponse(
        slug=topic.slug,
        conversation_id=str(conv.id),
        already_attached=already,
    )


@router.post("/{slug}/ask")
async def ask_followup(
    slug: str,
    body: FollowupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[BriefMessageReadout]:
    """Append a user question to the dossier thread and run a cited reply.

    The thread is anchored to the topic, not to a single brief
    generation — when the user regenerates the brief later, the
    conversation continues. Each message records which brief was
    current at the time for audit reconstruction.

    Returns the new pair of messages (user + assistant). The full
    thread is fetchable via GET /thread.
    """
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty question")

    topic = await _resolve_topic_or_404(db, slug)

    # Self-harm input guard. The user message gets persisted (so the
    # thread shows what was asked) but the assistant skips the LLM
    # round-trip and emits the safety response directly.
    if _is_self_harm(body.question):
        u_msg = BriefMessage(
            topic_id=topic.id, user_id=user.id, role="user", content=body.question
        )
        db.add(u_msg)
        await db.flush()
        a_msg = BriefMessage(
            topic_id=topic.id,
            user_id=user.id,
            role="assistant",
            content="",
            safety_response=_SELF_HARM_RESPONSE,
        )
        db.add(a_msg)
        await db.commit()
        await db.refresh(u_msg)
        await db.refresh(a_msg)
        return [_msg_readout(u_msg), _msg_readout(a_msg)]

    require_phi_consent(user)

    brief = await _latest_brief(db, topic.id)
    prior = await _thread_for_topic(db, topic.id)

    # Persist the user turn first so it's visible in the thread even if
    # the assistant call fails downstream.
    u_msg = BriefMessage(
        topic_id=topic.id,
        topic_brief_id=brief.id if brief else None,
        user_id=user.id,
        role="user",
        content=body.question,
    )
    db.add(u_msg)
    await db.commit()
    await db.refresh(u_msg)

    # Retrieval: combine the topic's own facts (alias + label_pattern
    # match) with free-text retrieval against the new question. Dedupe
    # by id, cap at 60 to keep the prompt bounded.
    topic_facts = await facts_for_topic(db, topic, limit=200)
    question_facts = await search_facts(db, body.question, limit=24, user_id=user.id)
    seen: dict[uuid.UUID, ExtractedFact] = {}
    for f in topic_facts + question_facts:
        if f.id not in seen:
            seen[f.id] = f
    combined = list(seen.values())[:60]

    prompt = get_registry().get("dossier_followup")
    user_vars = {
        "topic_name": topic.name,
        "topic_aliases": ", ".join(topic.aliases or []) or "(none)",
        "brief_block": _format_brief_for_prompt(brief),
        "prior_messages_block": _format_messages_for_prompt(prior),
        "facts_block": _format_facts_block(combined),
        "question": body.question,
    }
    result = await call_with_tool(
        db, user, prompt,
        user_vars=user_vars,
        purpose="dossier_followup",
        tool_name="emit_followup_answer",
    )
    out = result.tool_input or {}

    a_msg = BriefMessage(
        topic_id=topic.id,
        topic_brief_id=brief.id if brief else None,
        user_id=user.id,
        role="assistant",
        content=(out.get("answer") or "") if not result.error else "",
        citations=out.get("citations", []) or [],
        retrieved_fact_count=len(combined),
        model_run_id=result.model_run_id,
        safety_response=out.get("safety_response"),
        error=result.error,
    )
    db.add(a_msg)
    await db.commit()
    await db.refresh(a_msg)

    return [_msg_readout(u_msg), _msg_readout(a_msg)]
