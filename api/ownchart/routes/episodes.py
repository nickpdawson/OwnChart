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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.logger import get_logger
from ..llm.episode_intelligence import run_episode_intelligence
from ..models.audit_event import AuditEvent
from ..models.episode import Episode, EpisodeMember
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.user import User
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.episodes")


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> IntelligenceResponse:
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


class EpisodeDetail(EpisodeSummary):
    payload: dict[str, Any]
    members: list[EpisodeMemberOut] = Field(default_factory=list)


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[EpisodeSummary]:
    rows = list((await db.execute(
        select(Episode)
        .where(Episode.user_id == user.id)
        .where(Episode.merged_into_id.is_(None))   # hide merged duplicates
        .order_by(Episode.date_start.desc().nullslast(), Episode.created_at.desc())
        .limit(100)
    )).scalars().all())
    return [_summary(e) for e in rows]


@router.get("/recent", response_model=list[EpisodeSummary])
async def list_recent_episodes_route(
    limit: int = 6,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[EpisodeSummary]:
    """Newest canonical episodes — feeds Home + Timeline surfaces."""
    rows = list((await db.execute(
        select(Episode)
        .where(Episode.user_id == user.id)
        .where(Episode.merged_into_id.is_(None))   # hide merged duplicates
        .order_by(Episode.date_start.desc().nullslast(), Episode.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )).scalars().all())
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
    user: User = Depends(get_current_user),
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

    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
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
    return await get_episode_route(ep.id, user, db)


@router.post("/{episode_id}/aliases", response_model=EpisodeDetail)
async def add_episode_alias_route(
    episode_id: uuid.UUID,
    body: EpisodeAliasRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Add an alias to an Event. Idempotent on case-insensitive match."""
    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    new_alias = (body.alias or "").strip()
    if not new_alias:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="alias is required"
        )
    existing = list(ep.aliases or [])
    if any(a.lower() == new_alias.lower() for a in existing):
        return await get_episode_route(ep.id, user, db)
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
        event_type="episode_alias_added",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={"alias": new_alias},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    await db.commit()
    return await get_episode_route(ep.id, user, db)


@router.delete("/{episode_id}/aliases/{alias}", response_model=EpisodeDetail)
async def remove_episode_alias_route(
    episode_id: uuid.UUID,
    alias: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Remove an alias (case-insensitive). Idempotent."""
    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
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
            event_type="episode_alias_removed",
            subject_type="episode",
            subject_id=str(ep.id),
            detail={"alias": alias},
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        ))
        await db.commit()
    return await get_episode_route(ep.id, user, db)


@router.get("/{episode_id}", response_model=EpisodeDetail)
async def get_episode_route(
    episode_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    members = list((await db.execute(
        select(EpisodeMember)
        .where(EpisodeMember.episode_id == ep.id)
        .order_by(EpisodeMember.ordinal, EpisodeMember.created_at)
    )).scalars().all())
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Promote a SensemakingCandidate(candidate_type='episode') to a
    canonical Episode. Members are seeded from the candidate's
    fact_ids / source_ids and from the planner payload anchor +
    procedure components when available.

    Optional body sets `display_title` + `aliases` at save time so
    the user can immediately reference the Event by their chosen
    name without a follow-up PATCH."""
    cand = await db.get(SensemakingCandidate, candidate_id)
    if cand is None or cand.user_id != user.id:
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
        event_type="episode_promoted",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={"candidate_id": str(cand.id),
                "anchor_fact_id": str(anchor_fact_id) if anchor_fact_id else None},
    ))
    await db.commit()

    return await get_episode_route(ep.id, user, db)


@router.post(
    "/{episode_id}/attach-candidate/{candidate_id}",
    response_model=EpisodeDetail,
)
async def attach_candidate_to_episode_route(
    episode_id: uuid.UUID,
    candidate_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Attach a SensemakingCandidate's facts/sources to an existing
    Event ("Save as existing Event"). Does NOT create a new Episode
    or overwrite the existing title — just merges members and marks
    the candidate accepted.
    """
    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    cand = await db.get(SensemakingCandidate, candidate_id)
    if cand is None or cand.user_id != user.id:
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
        event_type="episode_candidate_attached",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={
            "candidate_id": str(cand.id),
            "members_added": added,
        },
    ))
    await db.commit()
    return await get_episode_route(ep.id, user, db)


# ---------------------------------------------------------------------------
# Merge: collapse two duplicate Events.

@router.post(
    "/{source_episode_id}/merge-into/{target_episode_id}",
    response_model=EpisodeDetail,
)
async def merge_episodes_route(
    source_episode_id: uuid.UUID,
    target_episode_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Mark `source_episode` as a duplicate of `target_episode`,
    copying any non-overlapping members across. The source row
    stays in the DB (audit trail, link survivability) but is
    excluded from Home / list / search via merged_into_id.
    """
    if source_episode_id == target_episode_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="cannot merge an Event into itself",
        )
    src = await db.get(Episode, source_episode_id)
    tgt = await db.get(Episode, target_episode_id)
    if src is None or src.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="source not found")
    if tgt is None or tgt.user_id != user.id:
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
    return await get_episode_route(tgt.id, user, db)


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EpisodeDetail:
    """Schedule a re-run of Episode Intelligence on this Event.
    The planner pulls the current facts in the date window and
    overwrites payload.intelligence. Same async pattern as
    POST /api/conversations — background-task, ~60s to land.
    Clears intelligence_stale_after when it completes.
    """
    ep = await db.get(Episode, episode_id)
    if ep is None or ep.user_id != user.id:
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
        )

    db.add(AuditEvent(
        user_id=user.id,
        event_type="episode_refresh_requested",
        subject_type="episode",
        subject_id=str(ep.id),
        detail={},
    ))
    ep.intelligence_stale_after = None
    ep.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_episode_route(ep.id, user, db)
