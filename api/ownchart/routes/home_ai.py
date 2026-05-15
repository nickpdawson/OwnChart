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

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
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


class HomeInsight(BaseModel):
    body: str
    question: str | None = None
    related_fact_ids: list[str] = Field(default_factory=list)
    generated_at: datetime


class HomeAiPartnerResponse(BaseModel):
    suggested_questions: list[SuggestedQuestion] = Field(default_factory=list)
    recent_conversations: list[RecentConversation] = Field(default_factory=list)
    recent_episodes: list[RecentEpisode] = Field(default_factory=list)
    make_sense_targets: list[MakeSenseTarget] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    # Proactive LLM-generated observation, ~2-4 sentences. None when
    # the model declined (sparse record) or generation failed.
    # Generated once per UTC day per user; cached in-process.
    insight: HomeInsight | None = None


# Insight cache: (user_id, YYYY-MM-DD) → HomeInsight | None. None
# means "we tried today and the model declined / failed — don't
# re-try until tomorrow". Single-process scope is fine for V1; if
# the API scales horizontally we'd move this to Redis.
_INSIGHT_CACHE: dict[tuple, HomeInsight | None] = {}


@router.get("/ai-partner", response_model=HomeAiPartnerResponse)
async def get_home_ai_partner(
    background_tasks: BackgroundTasks,
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
            # Prefer the user's display_title (Named Events) over the
            # planner-derived title. Falls back when not set.
            title=e.display_title or e.title,
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

    # -----------------------------------------------------------------
    # Proactive insight (LLM-generated, cached per UTC day per user).
    #
    # NEVER block the home response on the LLM call. The Anthropic
    # call costs 30-60s and the in-process cache is wiped on every
    # API restart (= every deploy), so on a cold cache the entire
    # Home page used to wait for the model. Now:
    #
    #   - Cache hit (insight ready): return it inline. Hot path is
    #     instant.
    #   - Cache miss: schedule the build as a BackgroundTask and
    #     return insight=null. The Home page renders without it;
    #     the user can refresh later to pick it up.
    #
    # `insight=null` is already the documented client contract
    # (HomeInsight | None) — the InsightCard component handles it.
    # -----------------------------------------------------------------
    key = (user.id, now.date().isoformat())
    if key in _INSIGHT_CACHE:
        out.insight = _INSIGHT_CACHE[key]
    else:
        background_tasks.add_task(
            _background_build_home_insight,
            user_id=user.id,
            day_iso=now.date().isoformat(),
        )

    return out


async def _background_build_home_insight(
    *, user_id: uuid.UUID, day_iso: str,
) -> None:
    """Runs the home_insight prompt out-of-band so the home GET
    returns immediately. Opens its own DB session because the
    request session is closed by the time this fires.
    """
    from ..core.db import SessionLocal
    from ..core.logger import get_logger
    log = get_logger("ownchart.routes.home_ai")
    try:
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user is None:
                return
            # Use today's UTC date — not the request's `now`, since
            # the request and the background fire are separated by
            # at most a few ms but the request's `now` is in scope.
            now = datetime.now(timezone.utc)
            # If the request thought today was day_iso but the
            # background ran past midnight UTC, we still build for
            # today's date — the cache key uses `now.date()`.
            del day_iso  # documentation; kept in signature for log lookup
            await _build_home_insight(db, user, now)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "home_insight_bg_failed",
            user_id=str(user_id), error=f"{type(e).__name__}: {e}",
        )


async def _build_home_insight(
    db: AsyncSession, user: User, now: datetime,
    *,
    force_refresh: bool = False,
) -> HomeInsight | None:
    """Run the home_insight prompt over recent record state and cache
    the result for the rest of the UTC day. Returns None when the
    cache says we already tried today, when the model declined, or
    when we have no facts to consider.

    `force_refresh=True` (manual "Make sense of what changed" trigger)
    bypasses the cache and adds a hint to the user_template focusing
    the model on what's new since the last refresh.
    """
    key = (user.id, now.date().isoformat())
    if not force_refresh and key in _INSIGHT_CACHE:
        return _INSIGHT_CACHE[key]

    cutoff_90 = now - timedelta(days=90)
    cutoff_30 = now - timedelta(days=30)

    recent_major = list((await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= cutoff_90)
        .where(ExtractedFact.review_state.in_(("confirmed", "corrected", "pattern_managed")))
        .where(ExtractedFact.significance.in_((
            "major_event", "major_procedure", "major_diagnosis", "major_medication",
        )))
        .order_by(ExtractedFact.date_start.desc())
        .limit(20)
    )).scalars().all())

    recent_all = list((await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.date_start.isnot(None))
        .where(ExtractedFact.date_start >= cutoff_30)
        .where(ExtractedFact.review_state.in_(("confirmed", "corrected", "pattern_managed")))
        .order_by(ExtractedFact.date_start.desc())
        .limit(40)
    )).scalars().all())

    topics_list = list((await db.execute(
        select(Topic).order_by(Topic.created_at.asc())
    )).scalars().all())

    recent_convo_titles = list((await db.execute(
        select(Conversation.title)
        .where(Conversation.user_id == user.id)
        .where(Conversation.archived.is_(False))
        .where(Conversation.title.isnot(None))
        .where(Conversation.created_at >= cutoff_30)
        .order_by(Conversation.created_at.desc())
        .limit(10)
    )).scalars().all())

    # If the record is too sparse to observe anything, don't even
    # spend the LLM call. Marker None in cache so we don't re-check
    # until tomorrow (skipped on force_refresh).
    if len(recent_all) < 5 and len(recent_major) == 0:
        if not force_refresh:
            _INSIGHT_CACHE[key] = None
        return None

    # Recent EHR/HK syncs (last 7d) — what's freshly arriving so the
    # model can lean into "since your Bridger sync this morning, …"
    # framing when relevant.
    cutoff_7 = now - timedelta(days=7)
    from ..models.provider_connection import ProviderConnection
    from ..models.provider_connector import ProviderConnector
    recent_syncs = list((await db.execute(
        select(ProviderConnection, ProviderConnector.name, ProviderConnector.ehr_vendor)
        .join(ProviderConnector,
              ProviderConnector.id == ProviderConnection.connector_id)
        .where(ProviderConnection.user_id == user.id)
        .where(ProviderConnection.last_synced_at.isnot(None))
        .where(ProviderConnection.last_synced_at >= cutoff_7)
        .order_by(ProviderConnection.last_synced_at.desc())
    )).all())

    # Active dossiers: topics with conversations in last 30d.
    # Conversation.scope is JSONB → dict in Python, which isn't
    # hashable, so we extract slugs into a plain set first instead of
    # set-ing the dicts directly (caught Nick's golden-path walk
    # 2026-05-13).
    scope_rows = list((await db.execute(
        select(Conversation.scope)
        .where(Conversation.user_id == user.id)
        .where(Conversation.archived.is_(False))
        .where(Conversation.last_message_at >= cutoff_30)
    )).scalars().all())
    active_slugs: set[str] = set()
    for sc in scope_rows:
        if isinstance(sc, dict):
            slug = sc.get("topic_slug")
            if isinstance(slug, str):
                active_slugs.add(slug)
    active_topics = [t for t in topics_list if t.slug in active_slugs]

    def _fmt_fact(f: ExtractedFact) -> str:
        d = f.date_start.date().isoformat() if f.date_start else "?"
        sig = f.significance or "background"
        return f"  - {d} | id={f.id} | {sig} | {f.fact_type} | {(f.display_label or f.label)[:120]}"

    def _fmt_sync(row) -> str:
        conn, name, vendor = row
        when = conn.last_synced_at.date().isoformat() if conn.last_synced_at else "?"
        counts = conn.cached_resource_counts or {}
        top_counts = ", ".join(f"{k}={v}" for k, v in sorted(
            counts.items(), key=lambda kv: -kv[1])[:5]) if counts else "no counts"
        return f"  - {when} | {name} ({vendor or 'unknown'}): {top_counts}"

    user_vars = {
        "today": now.date().isoformat(),
        "trigger_mode": "manual refresh" if force_refresh else "daily auto",
        "trigger_hint": (
            "The patient just clicked 'Make sense of what changed' — focus "
            "especially on what's new since they last looked at their record."
            if force_refresh else
            "Daily auto-render — observe whatever stands out about the "
            "current state."
        ),
        "recent_syncs": "\n".join(_fmt_sync(r) for r in recent_syncs)
            or "  (no syncs in the last 7 days)",
        "recent_major_facts": "\n".join(_fmt_fact(f) for f in recent_major)
            or "  (none)",
        "recent_all_facts": "\n".join(_fmt_fact(f) for f in recent_all[:20])
            or "  (none)",
        "active_dossiers": "\n".join(f"  - {t.name}" for t in active_topics)
            or "  (none with recent activity)",
        "topics": "\n".join(f"  - {t.name}" for t in topics_list)
            or "  (none yet)",
        "recent_conversation_titles": "\n".join(
            f"  - {t}" for t in recent_convo_titles
        ) or "  (none)",
    }

    from ..llm import call_with_tool, get_registry
    prompt = get_registry().get("home_insight")
    result = await call_with_tool(
        db, user, prompt,
        user_vars=user_vars,
        purpose="home_insight",
        tool_name="emit_home_insight",
    )
    if result.error and not result.tool_input:
        _INSIGHT_CACHE[key] = None
        return None
    out = result.tool_input or {}
    if out.get("refuse") or not out.get("body"):
        _INSIGHT_CACHE[key] = None
        return None

    insight = HomeInsight(
        body=str(out.get("body", "")).strip(),
        question=(str(out["question"]).strip() if out.get("question") else None),
        related_fact_ids=[
            str(fid) for fid in (out.get("related_fact_ids") or [])
            if isinstance(fid, str)
        ],
        generated_at=now,
    )
    _INSIGHT_CACHE[key] = insight
    return insight


@router.post("/insight/refresh", response_model=HomeInsight)
async def refresh_home_insight(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> HomeInsight:
    """Manual "Make sense of what changed" trigger. Bypasses the
    daily cache, runs the prompt with a force_refresh hint focused
    on recent change rather than generic observation, returns the
    fresh insight.

    Returns 422 if the record is too sparse for the model to observe
    anything (rather than the daily auto-render's silent skip).
    """
    now = datetime.now(timezone.utc)
    insight = await _build_home_insight(db, user, now, force_refresh=True)
    if insight is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Not enough recent activity to observe a meaningful change. "
                "Add more sources or wait for sync activity."
            ),
        )
    return insight
