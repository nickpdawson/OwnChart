"""Home AI partner endpoint (docs/10).

GET /api/home/ai-partner — the data the AI Partner header needs to
render. Suggested questions are deterministic and shaped by what
the user's record actually contains (we never invent a topic that
isn't on the record).

  {
    "suggested_questions": [{"visible_text", "submitted_text", "scope_hint", "scope"}, ...],
    "recent_conversations": [...up to 5],
    "make_sense_targets": [{"kind", "label", "href", "detail"}, ...],
    "providers": [{"key", "label", "configured"}, ...]
  }

Q-D2 (2026-05-11 PM): each suggested question carries BOTH a short
visible string the UI renders as a chip AND a longer submitted_text
that gets posted to /api/conversations when the user clicks. That
lets the chip read "Help me understand my May 1 surgery and recovery"
while the LLM receives the full evidence-rich prompt that includes
the travel/HRV/anesthesia angles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..llm.providers import available_providers
from ..models.conversation import Conversation
from ..models.episode import Episode
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.source_document import SourceDocument
from ..models.topic import Topic
from ..models.user import User
from .auth import get_current_user

router = APIRouter()


class SuggestedQuestion(BaseModel):
    visible_text: str
    submitted_text: str
    scope_hint: str
    scope: dict[str, Any] | None = None


class RecentConversation(BaseModel):
    id: str
    title: str | None
    kind: str
    provider: str | None
    model: str | None
    last_message_at: datetime | None


class MakeSenseTarget(BaseModel):
    # source | period | review_queue | dossier | unopened_import |
    # low_confidence_summary
    kind: str
    label: str
    href: str | None
    detail: str | None = None


class RecentEpisode(BaseModel):
    id: str
    title: str
    kind: str
    date_start: datetime | None
    summary: str | None


class HomeAiPartnerResponse(BaseModel):
    suggested_questions: list[SuggestedQuestion] = Field(default_factory=list)
    recent_conversations: list[RecentConversation] = Field(default_factory=list)
    recent_episodes: list[RecentEpisode] = Field(default_factory=list)
    make_sense_targets: list[MakeSenseTarget] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/ai-partner", response_model=HomeAiPartnerResponse)
async def get_home_ai_partner(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> HomeAiPartnerResponse:
    out = HomeAiPartnerResponse(providers=available_providers())
    now = datetime.now(timezone.utc)

    # ---------------------------------------------------------------
    # Suggested questions — phrasings live in
    # prompts/suggested_questions.v1.yaml so they can be revised
    # without a code deploy. This route only resolves the context
    # (most recent major event + top topic + current date) and asks
    # the loader to render the YAML against it.
    # ---------------------------------------------------------------

    most_recent_major = (await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.significance.in_(
            ("major_event", "major_procedure")
        ))
        .where(ExtractedFact.review_state.notin_(
            ("deferred", "rejected", "source_only")
        ))
        .order_by(ExtractedFact.date_start.desc())
        .limit(1)
    )).scalar_one_or_none()

    major_event_ctx: dict[str, Any] | None = None
    if most_recent_major is not None and most_recent_major.date_start is not None:
        d = most_recent_major.date_start
        days_ago = max(1, (now - d).days)
        when_short = d.strftime("%b %-d") if days_ago < 365 else d.strftime("%b %Y")
        phrase = (
            "about a week ago" if days_ago <= 10
            else "about a month ago" if days_ago <= 45
            else f"{days_ago} days ago"
        )
        label = most_recent_major.display_label or most_recent_major.label
        kind_word = most_recent_major.fact_type.replace("_", " ")
        major_event_ctx = {
            "event_label": label,
            "fact_type": kind_word,
            "fact_id": str(most_recent_major.id),
            "iso_date": d.date().isoformat(),
            "short_date": when_short,
            "phrase": phrase,
        }

    top_topic = (await db.execute(
        select(Topic).order_by(Topic.created_at.asc()).limit(1)
    )).scalar_one_or_none()
    topic_ctx: dict[str, Any] | None = None
    if top_topic is not None:
        topic_ctx = {
            "topic_name": top_topic.name,
            "topic_lower": top_topic.name.lower(),
            "topic_slug": top_topic.slug,
        }

    from ..llm.suggested_questions import render_suggested_questions
    rendered = render_suggested_questions(
        has_major_event=major_event_ctx is not None,
        major_event_ctx=major_event_ctx,
        has_top_topic=topic_ctx is not None,
        topic_ctx=topic_ctx,
        now=now,
    )
    out.suggested_questions = [
        SuggestedQuestion(
            visible_text=q["visible_text"],
            submitted_text=q["submitted_text"],
            scope_hint=q["scope_hint"],
            scope=q.get("scope"),
        )
        for q in rendered
    ]

    # ---------------------------------------------------------------
    # Recent conversations
    # ---------------------------------------------------------------
    convs = list((await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .where(Conversation.archived.is_(False))
        .order_by(Conversation.last_message_at.desc().nullslast(),
                  Conversation.created_at.desc())
        .limit(5)
    )).scalars().all())
    out.recent_conversations = [
        RecentConversation(
            id=str(c.id),
            title=c.title,
            kind=c.kind,
            provider=c.provider,
            model=c.model,
            last_message_at=c.last_message_at,
        )
        for c in convs
    ]

    # ---------------------------------------------------------------
    # Recent canonical Episodes — permanence on Home (2026-05-11 PM)
    # ---------------------------------------------------------------
    eps = list((await db.execute(
        select(Episode)
        .where(Episode.user_id == user.id)
        .order_by(Episode.date_start.desc().nullslast(), Episode.created_at.desc())
        .limit(6)
    )).scalars().all())
    out.recent_episodes = [
        RecentEpisode(
            id=str(e.id),
            title=e.title,
            kind=e.kind,
            date_start=e.date_start,
            summary=e.summary,
        )
        for e in eps
    ]

    # ---------------------------------------------------------------
    # Make Sense targets — Q-D1 expanded set
    # ---------------------------------------------------------------

    # a. Recent sources (last 30 days).
    cutoff_30 = now - timedelta(days=30)
    recent_sources = list((await db.execute(
        select(SourceDocument)
        .where(SourceDocument.acquired_at >= cutoff_30)
        .order_by(SourceDocument.acquired_at.desc())
        .limit(3)
    )).scalars().all())
    for s in recent_sources:
        label = s.source_label or s.original_filename or "Untitled source"
        out.make_sense_targets.append(MakeSenseTarget(
            kind="source",
            label=f"Make sense of {label}",
            href=f"/sources/{s.id}",
            detail=f"Ingested {s.acquired_at.date().isoformat()}",
        ))

    # b. Review queue (deterministic; always a target).
    out.make_sense_targets.append(MakeSenseTarget(
        kind="review_queue",
        label="Clean up the review queue",
        href="/review",
        detail="Group medication noise; surface decisions that change the story.",
    ))

    # c. Period digest (last 7 days).
    out.make_sense_targets.append(MakeSenseTarget(
        kind="period",
        label="Summarize the last 7 days",
        href="/timeline",
        detail="What landed and what changed in the past week.",
    ))

    # d. Recent dossiers with new evidence (last 14 days).
    cutoff_14 = now - timedelta(days=14)
    # Topics that have at least one recent fact through their normal
    # membership clause. Cheap approximation: topics with the most
    # recent fact-text matches via Topic.created_at + a count subquery
    # would be ideal, but for V1 we just list the two most-recent
    # topics — the topic-membership runtime is downstream of /dossier.
    dossier_rows = list((await db.execute(
        select(Topic)
        .order_by(Topic.created_at.desc())
        .limit(2)
    )).scalars().all())
    for t in dossier_rows:
        out.make_sense_targets.append(MakeSenseTarget(
            kind="dossier",
            label=f"Make sense of {t.name}",
            href=f"/dossier/{t.slug}",
            detail="Recent evidence may have changed the story.",
        ))

    # e. Low-confidence source-summary candidates (LLM ran but flagged
    #    inferred/unknown). Surface so the user can revise or rerun.
    low_conf = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.user_id == user.id)
        .where(SensemakingCandidate.candidate_type == "source_summary")
        .where(SensemakingCandidate.disposition == "pending")
        .where(SensemakingCandidate.claim_label.in_(("inferred", "unknown")))
        .order_by(SensemakingCandidate.created_at.desc())
        .limit(2)
    )).scalars().all())
    for c in low_conf:
        sid = (c.source_ids or [None])[0]
        out.make_sense_targets.append(MakeSenseTarget(
            kind="low_confidence_summary",
            label=f"Review weak summary: {c.title or '(untitled)'}",
            href=f"/sources/{sid}" if sid else None,
            detail=f"Marked {c.claim_label}; consider re-running with more evidence.",
        ))

    # f. Unopened imports — sources ingested in the last 30 days that
    #    have NO conversation thread scoped to them. Crude but useful.
    if recent_sources:
        recent_ids = [s.id for s in recent_sources]
        # Count conversations whose scope.source_ids JSON array
        # intersects the recent set. JSON containment is expensive on
        # large tables; for V1 we approximate by listing recent
        # conversations and checking client-side.
        conv_scopes = list((await db.execute(
            select(Conversation.scope)
            .where(Conversation.user_id == user.id)
            .where(Conversation.created_at >= cutoff_30)
        )).scalars().all())
        opened_ids: set[str] = set()
        for sc in conv_scopes:
            if not isinstance(sc, dict):
                continue
            for s in sc.get("source_ids", []) or []:
                opened_ids.add(str(s))
        for s in recent_sources:
            if str(s.id) in opened_ids:
                continue
            label = s.source_label or s.original_filename or "Untitled source"
            out.make_sense_targets.append(MakeSenseTarget(
                kind="unopened_import",
                label=f"Open {label}",
                href=f"/sources/{s.id}",
                detail="Ingested but you haven't asked about it yet.",
            ))

    return out
