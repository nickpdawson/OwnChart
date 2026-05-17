"""Episodes API — docs/10 + Nick 2026-05-11 PM (Episode Intelligence).

Endpoints:

  POST /api/episodes/intelligence  — run the planner + LLM end-to-end
                                     (anchor by fact_id / episode_id /
                                     natural-language phrase)
  GET  /api/episodes               — list canonical episodes
  GET  /api/episodes/{id}          — full episode + members
  POST /api/episodes/from-candidate/{candidate_id}
                                   — promote a SensemakingCandidate
                                     (candidate_type='episode') into a
                                     canonical Episode
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, get_auth_context, require_role
from ..core.config import get_settings
from ..core.db import get_session
from ..core.demo_session import demo_session_matches, get_demo_session_id
from ..core.logger import get_logger
from ..llm.episode_intelligence import run_episode_intelligence
from ..models.audit_event import AuditEvent
from ..models.episode import Episode, EpisodeMember
from ..models.sensemaking_candidate import SensemakingCandidate

router = APIRouter()
log = get_logger("ownchart.routes.episodes")


def _episode_visible_in_demo(ep: Episode, request: Request) -> bool:
    """Demo-mode visibility check for an Episode.

    True when:
      - we're not in demo mode (everything visible), OR
      - the episode is seeded (created_by != 'user'), OR
      - the episode is this visitor's own save (payload.demo_session_id
        matches the request's oc_demo_session cookie).

    False otherwise — the caller should treat that as 404, never
    disclosing the row's existence.
    """
    if not get_settings().demo_mode:
        return True
    if (ep.created_by or "") != "user":
        return True
    sid = get_demo_session_id(request)
    if not sid:
        return False
    payload = ep.payload if isinstance(ep.payload, dict) else {}
    return payload.get("demo_session_id") == sid


# ---------------------------------------------------------------------------
# Run Episode Intelligence


class IntelligenceRequest(BaseModel):
    fact_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    natural_language: str | None = None
    question: str | None = None


class IntelligenceResponse(BaseModel):
    job_id: str
    status: str
    conversation_id: str | None
    candidate: dict[str, Any] | None = None
    error: str | None = None


@router.post("/intelligence", response_model=IntelligenceResponse,
             status_code=status.HTTP_201_CREATED)
async def run_intelligence_route(
    body: IntelligenceRequest,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> IntelligenceResponse:
    user = ctx.user
    if not (body.fact_id or body.episode_id or body.natural_language):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="One of fact_id, episode_id, or natural_language is required.",
        )
    try:
        out = await run_episode_intelligence(
            db, user,
            fact_id=body.fact_id,
            episode_id=body.episode_id,
            natural_language=body.natural_language,
            question=body.question,
            person_record_id=ctx.active_record_id,
        )
    except Exception as e:  # noqa: BLE001
        # Log loudly with traceback so the underlying cause is
        # recoverable from container logs even when the response
        # body stays terse for prod safety.
        import traceback
        tb = traceback.format_exc()
        log.warning(
            "episode_intelligence_failed",
            error=f"{type(e).__name__}: {e}",
            traceback=tb,
        )
        # In dev / debug builds, surface the real exception so the
        # client (iOS, web) can show something actionable. Prod
        # stays generic to avoid leaking stack traces to anonymous
        # demo visitors.
        from ..core.config import get_settings as _settings_now
        s = _settings_now()
        if s.env == "dev" or s.debug_payloads:
            detail = f"Episode intelligence job failed: {type(e).__name__}: {e}"
        else:
            detail = "Episode intelligence job failed."
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        ) from e
    return IntelligenceResponse(**out)


# ---------------------------------------------------------------------------
# Canonical Episodes


class EpisodeMemberOut(BaseModel):
    id: str
    member_type: str
    subject_id: str
    role: str
    ordinal: int
    note: str | None


class EpisodeSummary(BaseModel):
    id: str
    title: str
    display_title: str | None = None
    aliases: list[str] = Field(default_factory=list)
    summary: str | None
    kind: str
    date_start: datetime | None
    date_end: datetime | None
    primary_fact_id: str | None
    created_by: str
    created_at: datetime


class RelatedConversationOut(BaseModel):
    id: str
    title: str | None
    kind: str
    last_message_at: datetime | None
    # How this conversation got linked: "member" = explicit attach via
    # episode_members; "anchor_fact" = EI conversation that stamped
    # scope.anchor_fact_id matching the Event's primary_fact_id.
    link_source: str


class EpisodeDetail(EpisodeSummary):
    payload: dict[str, Any]
    members: list[EpisodeMemberOut] = Field(default_factory=list)
    related_conversations: list[RelatedConversationOut] = Field(default_factory=list)


def _summary(e: Episode) -> EpisodeSummary:
    return EpisodeSummary(
        id=str(e.id),
        title=e.title,
        display_title=e.display_title,
        aliases=list(e.aliases or []),
        summary=e.summary,
        kind=e.kind,
        date_start=e.date_start,
        date_end=e.date_end,
        primary_fact_id=str(e.primary_fact_id) if e.primary_fact_id else None,
        created_by=e.created_by,
        created_at=e.created_at,
    )


@router.get("", response_model=list[EpisodeSummary])
async def list_episodes_route(
    request: Request,
    q: str | None = Query(default=None, description="Match against title, display_title, or any alias (ILIKE)."),
    kind: str | None = Query(default=None),
    date_from: str | None = Query(default=None, description="ISO date — only include events on/after this date."),
    date_to: str | None = Query(default=None, description="ISO date — only include events on/before this date."),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> list[EpisodeSummary]:
    """List Events, optionally filtered for an attach-picker UI.

    `q` matches title / display_title / aliases (Postgres array @>
    pattern) case-insensitively. `kind` is exact match. `date_from` /
    `date_to` filter on `date_start` and accept ISO `YYYY-MM-DD`.
    Merged events stay hidden.
    """
    user = ctx.user
    # M02 perimeter: scope by both user_id (actor) AND
    # person_record_id (target record). Demo-mode filter layers
    # underneath this.
    stmt = (
        select(Episode)
        .where(Episode.user_id == user.id)
        .where(Episode.person_record_id == ctx.active_record_id)
        .where(Episode.merged_into_id.is_(None))
        .order_by(Episode.date_start.desc().nullslast(), Episode.created_at.desc())
        .limit(limit)
    )
    if get_settings().demo_mode:
        # Hide visitor-saved events (created_by='user') from other
        # demo visitors. The shared demo account is otherwise leaky.
        # Seeded events (created_by='imported' / 'llm' / 'heuristic')
        # stay visible to everyone. A visitor's own saved events are
        # gated by payload->>demo_session_id below.
        sid = get_demo_session_id(request)
        if sid:
            stmt = stmt.where(
                text(
                    "(episodes.created_by != 'user' "
                    "OR episodes.payload->>'demo_session_id' = :dsid)"
                ).bindparams(dsid=sid)
            )
        else:
            stmt = stmt.where(text("episodes.created_by != 'user'"))
    if q:
        from sqlalchemy import func, or_
        pat = f"%{q.strip()}%"
        # Postgres ARRAY ILIKE pattern: unnest and any() match. Cast
        # aliases to text[] is already typed; use a subquery EXISTS so
        # we don't multiply rows.
        alias_match = text(
            "EXISTS (SELECT 1 FROM unnest(episodes.aliases) a "
            "WHERE a ILIKE :pat)"
        ).bindparams(pat=pat)
        stmt = stmt.where(or_(
            func.lower(Episode.title).like(func.lower(pat)),
            func.lower(Episode.display_title).like(func.lower(pat)),
            alias_match,
        ))
    if kind:
        stmt = stmt.where(Episode.kind == kind)
    if date_from:
        try:
            d = datetime.fromisoformat(date_from)
            stmt = stmt.where(Episode.date_start >= d)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"date_from must be ISO date, got {date_from!r}",
            )
    if date_to:
        try:
            d = datetime.fromisoformat(date_to)
            stmt = stmt.where(Episode.date_start <= d)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"date_to must be ISO date, got {date_to!r}",
            )

    rows = list((await db.execute(stmt)).scalars().all())
    return [_summary(e) for e in rows]


@router.get("/recent", response_model=list[EpisodeSummary])
async def list_recent_episodes_route(
    request: Request,
    limit: int = 6,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> list[EpisodeSummary]:
    """Newest canonical episodes — feeds Home + Timeline surfaces."""
    user = ctx.user
    stmt = (
        select(Episode)
        .where(Episode.user_id == user.id)
        .where(Episode.person_record_id == ctx.active_record_id)
        .where(Episode.merged_into_id.is_(None))   # hide merged duplicates
        .order_by(Episode.date_start.desc().nullslast(), Episode.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )
    if get_settings().demo_mode:
        sid = get_demo_session_id(request)
        if sid:
            stmt = stmt.where(
                text(
                    "(episodes.created_by != 'user' "
                    "OR episodes.payload->>'demo_session_id' = :dsid)"
                ).bindparams(dsid=sid)
            )
        else:
            stmt = stmt.where(text("episodes.created_by != 'user'"))
    rows = list((await db.execute(stmt)).scalars().all())
    return [_summary(e) for e in rows]


class EpisodePatchRequest(BaseModel):
    title: str | None = None
    # Rename for display. Backing `title` stays as the planner-derived
    # label so re-runs of EI don't fight the user.
    display_title: str | None = None
    # Replace the alias set wholesale. Pass [] to clear; omit to leave
    # untouched. Use POST/DELETE on /aliases for additive ops.
    aliases: list[str] | None = None
    summary: str | None = None
    kind: str | None = None
    # User-confirmable significance applied to the primary_fact_id.
    # major_event / major_procedure etc. lift the episode in every
    # ranked feed; background / source_only hide it.
    significance: str | None = None
    reason: str | None = None


class EpisodeAliasRequest(BaseModel):
    alias: str


@router.patch("/{episode_id}", response_model=EpisodeDetail)
async def patch_episode_route(
    episode_id: uuid.UUID,
    body: EpisodePatchRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Rename, retag (kind), or mark significance on an Episode.

    Significance writes through to the underlying `primary_fact_id`
    via the existing user-override path so Home, Timeline, Discover,
    Fact Context, and the notable feed all pick it up without a
    separate Episode-significance column.
    """
    from datetime import timezone as _tz
    from ..canonical.significance import RANK
    from ..models.extracted_fact import ExtractedFact

    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    now = datetime.now(_tz.utc)
    detail: dict[str, Any] = {}
    if body.title is not None and body.title.strip():
        detail["from_title"] = ep.title
        ep.title = body.title.strip()[:512]
    if body.display_title is not None:
        detail["from_display_title"] = ep.display_title
        cleaned = body.display_title.strip()
        ep.display_title = cleaned[:512] if cleaned else None
    if body.aliases is not None:
        detail["from_aliases"] = list(ep.aliases or [])
        # Dedupe + strip whitespace + drop empties + cap at 16.
        seen: set[str] = set()
        cleaned_aliases: list[str] = []
        for a in body.aliases:
            s = (a or "").strip()
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            cleaned_aliases.append(s[:128])
            if len(cleaned_aliases) >= 16:
                break
        ep.aliases = cleaned_aliases
    if body.summary is not None:
        ep.summary = body.summary
    if body.kind is not None and body.kind.strip():
        detail["from_kind"] = ep.kind
        ep.kind = body.kind.strip()[:48]
    if body.significance is not None:
        if body.significance not in RANK:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"significance must be one of {sorted(RANK.keys())}",
            )
        if ep.primary_fact_id is not None:
            f = await db.get(ExtractedFact, ep.primary_fact_id)
            if f is not None:
                detail["from_significance"] = f.significance
                f.significance = body.significance
                f.significance_source = "user"
                f.significance_set_at = now
                detail["to_significance"] = body.significance
                detail["primary_fact_id"] = str(f.id)

    ep.updated_at = now
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_patched",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={
            **detail,
            "reason": body.reason,
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    await db.commit()
    return await get_episode_route(ep.id, ctx, db)


@router.post("/{episode_id}/aliases", response_model=EpisodeDetail)
async def add_episode_alias_route(
    episode_id: uuid.UUID,
    body: EpisodeAliasRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Add an alias to an Event. Idempotent on case-insensitive match."""
    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    new_alias = (body.alias or "").strip()
    if not new_alias:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="alias is required"
        )
    existing = list(ep.aliases or [])
    if any(a.lower() == new_alias.lower() for a in existing):
        return await get_episode_route(ep.id, ctx, db)
    if len(existing) >= 16:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="alias cap reached (16). Remove one first.",
        )
    existing.append(new_alias[:128])
    ep.aliases = existing
    ep.updated_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_alias_added",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={"alias": new_alias},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    await db.commit()
    return await get_episode_route(ep.id, ctx, db)


@router.delete("/{episode_id}/aliases/{alias}", response_model=EpisodeDetail)
async def remove_episode_alias_route(
    episode_id: uuid.UUID,
    alias: str,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Remove an alias (case-insensitive). Idempotent."""
    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    target = (alias or "").strip().lower()
    if not target:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY)
    before = list(ep.aliases or [])
    after = [a for a in before if a.lower() != target]
    if after != before:
        ep.aliases = after
        ep.updated_at = datetime.now(timezone.utc)
        db.add(AuditEvent(
            user_id=user.id,
            person_record_id=ctx.active_record_id,
            event_type="episode_alias_removed",
            subject_type="episode",
            subject_id=str(ep.id),
            detail={"alias": alias},
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        ))
        await db.commit()
    return await get_episode_route(ep.id, ctx, db)


@router.get("/{episode_id}", response_model=EpisodeDetail)
async def get_episode_route(
    episode_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]
) -> EpisodeDetail:
    from ..models.conversation import Conversation

    user = ctx.user
    ep = await db.get(Episode, episode_id)
    # M02 perimeter: 404 on cross-record so existence isn't disclosed.
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # request is None when called internally after a verified mutation
    # (alias add/remove etc.); HTTP entry always supplies the request.
    if request is not None and not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    members = list((await db.execute(
        select(EpisodeMember)
        .where(EpisodeMember.episode_id == ep.id)
        .order_by(EpisodeMember.ordinal, EpisodeMember.created_at)
    )).scalars().all())

    # Conversations explicitly attached via episode_members.
    # M02 perimeter: scope by active record so a stale cross-record
    # episode_member subject_id (pre-migration) can't dredge up a
    # conversation from a sibling record.
    member_conv_ids = [m.subject_id for m in members if m.member_type == "conversation"]
    related_by_member: list[Conversation] = []
    if member_conv_ids:
        related_by_member = list((await db.execute(
            select(Conversation)
            .where(Conversation.id.in_(member_conv_ids))
            .where(Conversation.person_record_id == ctx.active_record_id)
        )).scalars().all())

    # Conversations linked implicitly via scope.anchor_fact_id matching
    # this Event's primary_fact_id (legacy EI behavior).
    related_by_anchor: list[Conversation] = []
    if ep.primary_fact_id is not None:
        related_by_anchor = list((await db.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .where(Conversation.person_record_id == ctx.active_record_id)
            .where(Conversation.archived.is_(False))
            .where(text("conversations.scope->>'anchor_fact_id' = :afid")
                   .bindparams(afid=str(ep.primary_fact_id)))
            .order_by(Conversation.last_message_at.desc().nullslast())
        )).scalars().all())

    # Merge, dedupe on id, prefer member attribution for source label.
    seen_ids: set[uuid.UUID] = set()
    related_out: list[RelatedConversationOut] = []
    for c in related_by_member:
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        related_out.append(RelatedConversationOut(
            id=str(c.id), title=c.title, kind=c.kind,
            last_message_at=c.last_message_at, link_source="member",
        ))
    for c in related_by_anchor:
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        related_out.append(RelatedConversationOut(
            id=str(c.id), title=c.title, kind=c.kind,
            last_message_at=c.last_message_at, link_source="anchor_fact",
        ))

    return EpisodeDetail(
        **_summary(ep).model_dump(),
        payload=ep.payload or {},
        members=[
            EpisodeMemberOut(
                id=str(m.id), member_type=m.member_type,
                subject_id=str(m.subject_id), role=m.role,
                ordinal=m.ordinal, note=m.note,
            )
            for m in members
        ],
        related_conversations=related_out,
    )


class PromoteCandidateRequest(BaseModel):
    """Optional rename payload for save-as-Event.

    Lets the UI ship "Save as new Event with name 'X' and aliases [...]"
    in a single request instead of POST-then-PATCH. Backing `title`
    stays as the planner-derived label; display_title is the friendly
    one the user typed.
    """
    display_title: str | None = None
    aliases: list[str] | None = None


@router.post("/from-candidate/{candidate_id}", response_model=EpisodeDetail,
             status_code=status.HTTP_201_CREATED)
async def promote_candidate_route(
    candidate_id: uuid.UUID,
    body: PromoteCandidateRequest | None = None,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Promote a SensemakingCandidate(candidate_type='episode') to a
    canonical Episode. Members are seeded from the candidate's
    fact_ids / source_ids and from the planner payload anchor +
    procedure components when available.

    Optional body sets `display_title` + `aliases` at save time so
    the user can immediately reference the Event by their chosen
    name without a follow-up PATCH."""
    user = ctx.user
    cand = await db.get(SensemakingCandidate, candidate_id)
    if (
        cand is None
        or cand.user_id != user.id
        or cand.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if cand.candidate_type != "episode":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"candidate_type must be 'episode', got '{cand.candidate_type}'",
        )

    now = datetime.now(timezone.utc)
    planner = (cand.payload or {}).get("planner") or {}
    anchor = planner.get("anchor") or {}
    structured = (cand.payload or {}).get("structured") or {}

    anchor_fact_id = None
    if anchor.get("fact_id"):
        try:
            anchor_fact_id = uuid.UUID(anchor["fact_id"])
        except (TypeError, ValueError):
            anchor_fact_id = None
    anchor_date = None
    if anchor.get("date_start"):
        try:
            anchor_date = datetime.fromisoformat(anchor["date_start"])
        except ValueError:
            anchor_date = None

    # Honor the rename + alias payload if present.
    display_title: str | None = None
    aliases_clean: list[str] = []
    if body is not None:
        if body.display_title is not None and body.display_title.strip():
            display_title = body.display_title.strip()[:512]
        if body.aliases:
            seen: set[str] = set()
            for a in body.aliases:
                s = (a or "").strip()
                if not s or s.lower() in seen:
                    continue
                seen.add(s.lower())
                aliases_clean.append(s[:128])
                if len(aliases_clean) >= 16:
                    break

    ep = Episode(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        title=cand.title or "Episode",
        display_title=display_title,
        aliases=aliases_clean,
        summary=cand.summary_text,
        kind="surgery" if "surger" in (cand.title or "").lower() else "other",
        date_start=anchor_date,
        date_end=anchor_date,  # V1 single-day; multi-day promotion comes later
        primary_fact_id=anchor_fact_id,
        promoted_from_candidate_id=cand.id,
        created_by="user",
        payload={
            "intelligence": structured,
            "follow_up_questions": (cand.payload or {}).get("follow_up_questions") or [],
        },
        created_at=now,
        updated_at=now,
    )
    db.add(ep)
    await db.flush()

    # Seed members: anchor (primary), same-day procedures (component),
    # same-day conditions (context), travel events (context).
    #
    # Dedup on (member_type, subject_id) — uq_episode_members_unique
    # enforces uniqueness on (episode_id, member_type, subject_id), and
    # the planner can emit the same source twice (once per fact-anchor
    # that points to it). Without this dedup the bulk insert hit
    # IntegrityError and Save-as-Episode 500'd for any episode whose
    # planner produced multi-pointed sources. Caught during golden-path
    # walk 2026-05-13.
    seen: set[tuple[str, uuid.UUID]] = set()

    def add_member(member_type: str, subject_id: uuid.UUID, role: str, ordinal: int) -> None:
        key = (member_type, subject_id)
        if key in seen:
            return
        seen.add(key)
        db.add(EpisodeMember(
            episode_id=ep.id,
            member_type=member_type,
            subject_id=subject_id,
            role=role,
            ordinal=ordinal,
            created_at=now,
        ))

    if anchor_fact_id is not None:
        add_member("fact", anchor_fact_id, "primary", 0)

    ordinal = 1
    for f in (planner.get("what_happened", {}).get("procedures") or []):
        try:
            fid = uuid.UUID(f["fact_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if fid == anchor_fact_id:
            continue
        add_member("fact", fid, "component", ordinal)
        ordinal += 1
    for f in (planner.get("anesthesia_meds", {}).get("facts") or []):
        try:
            fid = uuid.UUID(f["fact_id"])
        except (TypeError, ValueError, KeyError):
            continue
        add_member("fact", fid, "component", ordinal)
        ordinal += 1
    for f in (planner.get("travel_and_life", {}).get("events") or []):
        try:
            fid = uuid.UUID(f["fact_id"])
        except (TypeError, ValueError, KeyError):
            continue
        add_member("fact", fid, "context", ordinal)
        ordinal += 1
    for s in (planner.get("what_happened", {}).get("sources") or []):
        try:
            sid = uuid.UUID(s["source_id"])
        except (TypeError, ValueError, KeyError):
            continue
        add_member("source", sid, "component", ordinal)
        ordinal += 1

    cand.disposition = "accepted"
    cand.disposition_at = now

    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_promoted",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={"candidate_id": str(cand.id),
                "anchor_fact_id": str(anchor_fact_id) if anchor_fact_id else None},
    ))
    await db.commit()

    return await get_episode_route(ep.id, ctx, db)


@router.post(
    "/{episode_id}/attach-candidate/{candidate_id}",
    response_model=EpisodeDetail,
)
async def attach_candidate_to_episode_route(
    episode_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Attach a SensemakingCandidate's facts/sources to an existing
    Event ("Save as existing Event"). Does NOT create a new Episode
    or overwrite the existing title — just merges members and marks
    the candidate accepted.
    """
    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    if not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    cand = await db.get(SensemakingCandidate, candidate_id)
    if (
        cand is None
        or cand.user_id != user.id
        or cand.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    if cand.candidate_type != "episode":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"candidate_type must be 'episode', got '{cand.candidate_type}'",
        )

    now = datetime.now(timezone.utc)
    planner = (cand.payload or {}).get("planner") or {}
    anchor = planner.get("anchor") or {}

    # Reuse the existing-member dedup so we don't violate
    # uq_episode_members_unique when merging into an Event that
    # already has overlapping facts.
    existing = list((await db.execute(
        select(EpisodeMember.member_type, EpisodeMember.subject_id)
        .where(EpisodeMember.episode_id == ep.id)
    )).all())
    seen: set[tuple[str, uuid.UUID]] = {
        (mt, sid) for (mt, sid) in existing
    }

    def add_member(member_type: str, subject_id: uuid.UUID, role: str, ordinal: int) -> None:
        key = (member_type, subject_id)
        if key in seen:
            return
        seen.add(key)
        db.add(EpisodeMember(
            episode_id=ep.id,
            member_type=member_type,
            subject_id=subject_id,
            role=role,
            ordinal=ordinal,
            created_at=now,
        ))

    added = 0
    if anchor.get("fact_id"):
        try:
            add_member("fact", uuid.UUID(anchor["fact_id"]), "context",
                       len(seen))
            added += 1
        except (TypeError, ValueError):
            pass
    for f in (planner.get("what_happened", {}).get("procedures") or []):
        try:
            add_member("fact", uuid.UUID(f["fact_id"]), "context",
                       len(seen))
            added += 1
        except (TypeError, ValueError, KeyError):
            continue
    for s in (planner.get("what_happened", {}).get("sources") or []):
        try:
            add_member("source", uuid.UUID(s["source_id"]), "component",
                       len(seen))
            added += 1
        except (TypeError, ValueError, KeyError):
            continue

    cand.disposition = "accepted"
    cand.disposition_at = now
    ep.updated_at = now

    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_candidate_attached",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={
            "candidate_id": str(cand.id),
            "members_added": added,
        },
    ))
    await db.commit()
    return await get_episode_route(ep.id, ctx, db)


# ---------------------------------------------------------------------------
# Direct save / attach from a Conversation. These power the chat Save
# menu without requiring an Episode Intelligence candidate to exist —
# any chat (Ask, dossier_followup, etc.) can become an Event via
# /save-as-event or attach to an existing one via /attach-conversation.


class SaveAsEventRequest(BaseModel):
    """Direct save of a Conversation to a new Event, without an EI candidate."""
    title: str
    display_title: str | None = None
    aliases: list[str] | None = None
    summary: str | None = None
    kind: str | None = None  # surgery | diagnosis | medication | other
    date_start: str | None = None  # ISO date


class SaveAsEventResponse(BaseModel):
    episode_id: str


@router.post("/from-conversation/{conv_id}", response_model=SaveAsEventResponse,
             status_code=status.HTTP_201_CREATED)
async def save_conversation_as_event_route(
    conv_id: uuid.UUID,
    body: SaveAsEventRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> SaveAsEventResponse:
    """Create a new Event whose first member is this Conversation.

    No LLM, no candidate. Adds the conversation as a primary
    `episode_member(member_type='conversation')` so the Event
    detail page surfaces it under "Conversations about this Event".
    """
    from ..models.conversation import Conversation

    user = ctx.user
    conv = await db.get(Conversation, conv_id)
    if (
        conv is None
        or conv.user_id != user.id
        or conv.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if not demo_session_matches(conv.scope, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    title = body.title.strip()
    if not title:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title is required",
        )

    aliases_clean: list[str] = []
    if body.aliases:
        seen: set[str] = set()
        for a in body.aliases:
            s = (a or "").strip()
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            aliases_clean.append(s[:128])
            if len(aliases_clean) >= 16:
                break

    date_start = None
    if body.date_start:
        try:
            date_start = datetime.fromisoformat(body.date_start)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"date_start must be ISO date, got {body.date_start!r}",
            )

    now = datetime.now(timezone.utc)
    payload: dict = {}
    # Demo-mode: stamp the visitor's session id so they (and only
    # they) can see/edit this saved event later.
    if get_settings().demo_mode:
        sid = get_demo_session_id(request)
        if sid:
            payload["demo_session_id"] = sid
    ep = Episode(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        title=title[:512],
        display_title=(body.display_title or "").strip()[:512] or None,
        aliases=aliases_clean,
        summary=(body.summary or "").strip() or None,
        kind=(body.kind or "other").strip()[:48],
        date_start=date_start,
        date_end=date_start,
        primary_fact_id=None,
        created_by="user",
        payload=payload,
        created_at=now,
        updated_at=now,
    )
    db.add(ep)
    await db.flush()

    db.add(EpisodeMember(
        episode_id=ep.id,
        member_type="conversation",
        subject_id=conv.id,
        role="primary",
        ordinal=0,
        created_at=now,
    ))

    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_saved_from_conversation",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={"conversation_id": str(conv.id), "title": title},
    ))
    await db.commit()
    return SaveAsEventResponse(episode_id=str(ep.id))


class AttachConversationRequest(BaseModel):
    conversation_id: uuid.UUID


@router.post("/{episode_id}/attach-conversation", response_model=EpisodeDetail)
async def attach_conversation_to_episode_route(
    episode_id: uuid.UUID,
    body: AttachConversationRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Add an existing Conversation as a member of an Event.

    Idempotent: the (episode_id, member_type, subject_id) unique
    constraint silently swallows duplicates. The conversation's
    own scope is left alone — conversations can be attached to
    multiple Events without overwriting any topic_slug or
    anchor_fact_id that may already live there.
    """
    from ..models.conversation import Conversation

    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    if not _episode_visible_in_demo(ep, request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    if ep.merged_into_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Event is merged into another; attach to the canonical one",
        )
    conv = await db.get(Conversation, body.conversation_id)
    # M02 perimeter: can only attach a conversation from the same
    # active record. Cross-record attach would create a backdoor for
    # a caregiver to surface a sibling-record's conversation under a
    # parent's Event.
    if (
        conv is None
        or conv.user_id != user.id
        or conv.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    existing = (await db.execute(
        select(EpisodeMember.id)
        .where(EpisodeMember.episode_id == ep.id)
        .where(EpisodeMember.member_type == "conversation")
        .where(EpisodeMember.subject_id == conv.id)
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        db.add(EpisodeMember(
            episode_id=ep.id,
            member_type="conversation",
            subject_id=conv.id,
            role="context",
            ordinal=0,
            created_at=now,
        ))
        db.add(AuditEvent(
            user_id=user.id,
            person_record_id=ctx.active_record_id,
            event_type="episode_conversation_attached",
            subject_type="episode",
            subject_id=str(ep.id),
            detail={"conversation_id": str(conv.id)},
        ))
        ep.updated_at = now
    await db.commit()
    return await get_episode_route(ep.id, ctx, db)


# ---------------------------------------------------------------------------
# Merge: collapse two duplicate Events.

@router.post(
    "/{source_episode_id}/merge-into/{target_episode_id}",
    response_model=EpisodeDetail,
)
async def merge_episodes_route(
    source_episode_id: uuid.UUID,
    target_episode_id: uuid.UUID,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Mark `source_episode` as a duplicate of `target_episode`,
    copying any non-overlapping members across. The source row
    stays in the DB (audit trail, link survivability) but is
    excluded from Home / list / search via merged_into_id.
    """
    user = ctx.user
    if source_episode_id == target_episode_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="cannot merge an Event into itself",
        )
    src = await db.get(Episode, source_episode_id)
    tgt = await db.get(Episode, target_episode_id)
    # Both events must live on the same active record. A cross-record
    # merge would silently move members across records.
    if (
        src is None
        or src.user_id != user.id
        or src.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="source not found")
    if (
        tgt is None
        or tgt.user_id != user.id
        or tgt.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target not found")
    if tgt.merged_into_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="target is itself merged — pick the canonical Event",
        )

    now = datetime.now(timezone.utc)

    # Copy source's members to target (deduped on
    # uq_episode_members_unique).
    seen = {
        (mt, sid)
        for (mt, sid) in (await db.execute(
            select(EpisodeMember.member_type, EpisodeMember.subject_id)
            .where(EpisodeMember.episode_id == tgt.id)
        )).all()
    }
    src_members = list((await db.execute(
        select(EpisodeMember).where(EpisodeMember.episode_id == src.id)
    )).scalars().all())
    moved = 0
    for m in src_members:
        if (m.member_type, m.subject_id) in seen:
            continue
        db.add(EpisodeMember(
            episode_id=tgt.id,
            member_type=m.member_type,
            subject_id=m.subject_id,
            role=m.role,
            ordinal=m.ordinal,
            note=m.note,
            created_at=now,
        ))
        seen.add((m.member_type, m.subject_id))
        moved += 1

    # Pull source's aliases into target so referring by either
    # display_title keeps resolving.
    tgt_aliases = list(tgt.aliases or [])
    seen_a = {a.lower() for a in tgt_aliases}
    for a in (src.aliases or []):
        if a.lower() not in seen_a:
            tgt_aliases.append(a)
            seen_a.add(a.lower())
    if src.display_title and src.display_title.lower() not in seen_a:
        tgt_aliases.append(src.display_title)
    tgt.aliases = tgt_aliases[:16]

    src.merged_into_id = tgt.id
    src.updated_at = now
    tgt.updated_at = now

    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_merged",
        subject_type="episode",
        subject_id=str(tgt.id),
        detail={
            "source_episode_id": str(src.id),
            "target_episode_id": str(tgt.id),
            "members_moved": moved,
        },
    ))
    await db.commit()
    return await get_episode_route(tgt.id, ctx, db)


# ---------------------------------------------------------------------------
# Refresh intelligence — re-run EI on an Event whose underlying facts
# have changed since the last save (e.g. clinical-note backfill added
# new facts in the window).

@router.post(
    "/{episode_id}/refresh-intelligence",
    response_model=EpisodeDetail,
)
async def refresh_episode_intelligence_route(
    episode_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Schedule a re-run of Episode Intelligence on this Event.
    The planner pulls the current facts in the date window and
    overwrites payload.intelligence. Same async pattern as
    POST /api/conversations — background-task, ~60s to land.
    Clears intelligence_stale_after when it completes.
    """
    user = ctx.user
    ep = await db.get(Episode, episode_id)
    if (
        ep is None
        or ep.user_id != user.id
        or ep.person_record_id != ctx.active_record_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if ep.merged_into_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Event is merged into another; refresh the canonical one",
        )

    from ..llm.episode_intelligence import (
        run_episode_intelligence_in_background,
    )
    # Schedule a fresh planner+LLM run. The runtime resolves the
    # anchor by primary_fact_id and writes the new intelligence
    # into a fresh Conversation. After the run, we'd ideally also
    # copy payload.intelligence into THIS Event's payload — but
    # for v1, the Conversation IS the refreshed answer; the user
    # can re-promote / re-attach if they want it on the Event row.
    # Future: a separate path that overwrites Event.payload in-place.
    if ep.primary_fact_id is not None:
        background_tasks.add_task(
            run_episode_intelligence_in_background,
            conversation_id=uuid.uuid4(),  # NB: see comment in helper
            user_id=user.id,
            natural_language=ep.display_title or ep.title,
            person_record_id=ctx.active_record_id,
        )

    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=ctx.active_record_id,
        event_type="episode_refresh_requested",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={},
    ))
    ep.intelligence_stale_after = None
    ep.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_episode_route(ep.id, ctx, db)
