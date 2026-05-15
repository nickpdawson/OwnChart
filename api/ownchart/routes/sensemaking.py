"""Sensemaking endpoints — docs/08.

V1 ships three routes end-to-end:

  POST /api/sources/{source_id}/sensemake — run a source_summary job
  GET  /api/sensemaking/jobs/{job_id}      — poll status + result
  GET  /api/sources/{source_id}/candidates — pending candidates for a source

The candidate table is polymorphic; this route surfaces only what the
UI needs (the summary candidate + its episode-candidate siblings).
Promoting a candidate (creating Episode rows, marking facts
source-only) is a separate explicit step the UI will add later.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.logger import get_logger
from ..llm.sensemaking import SensemakingError, summarize_source
from ..models.audit_event import AuditEvent
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.sensemaking_job import SensemakingJob
from ..models.user import User
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.sensemaking")


class CandidateOut(BaseModel):
    id: str
    candidate_type: str
    title: str | None
    summary_text: str | None
    payload: dict
    claim_label: str | None
    confidence: int | None
    source_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    evidence_anchor_ids: list[str] = Field(default_factory=list)
    disposition: str
    disposition_at: datetime | None
    user_edit: str | None
    created_at: datetime


class JobOut(BaseModel):
    id: str
    job_type: str
    status: str
    privacy_mode: str
    scope: dict
    model_run_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    candidates: list[CandidateOut] = Field(default_factory=list)


def _candidate_to_out(c: SensemakingCandidate) -> CandidateOut:
    return CandidateOut(
        id=str(c.id),
        candidate_type=c.candidate_type,
        title=c.title,
        summary_text=c.summary_text,
        payload=c.payload or {},
        claim_label=c.claim_label,
        confidence=c.confidence,
        source_ids=[str(s) for s in (c.source_ids or [])],
        fact_ids=[str(f) for f in (c.fact_ids or [])],
        evidence_anchor_ids=[str(a) for a in (c.evidence_anchor_ids or [])],
        disposition=c.disposition,
        disposition_at=c.disposition_at,
        user_edit=c.user_edit,
        created_at=c.created_at,
    )


def _job_to_out(job: SensemakingJob, candidates: list[SensemakingCandidate]) -> JobOut:
    return JobOut(
        id=str(job.id),
        job_type=job.job_type,
        status=job.status,
        privacy_mode=job.privacy_mode,
        scope=job.scope or {},
        model_run_id=str(job.model_run_id) if job.model_run_id else None,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
        candidates=[_candidate_to_out(c) for c in candidates],
    )


@router.post("/review/medication-patterns", response_model=JobOut,
             status_code=status.HTTP_201_CREATED)
async def run_medication_pattern_triage(
    min_group_size: int = 5,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JobOut:
    """Group medication / symptom log entries into pattern candidates.

    Deterministic, no LLM, no PHI leaves the host. Reads `?min_group_size=`
    (default 5) — groups with fewer entries are skipped.
    """
    from ..llm.medication_triage import triage_medication_patterns
    job = await triage_medication_patterns(
        db, user, min_group_size=min_group_size,
    )
    candidates = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.job_id == job.id)
        .order_by(SensemakingCandidate.created_at.asc())
    )).scalars().all())
    return _job_to_out(job, candidates)


@router.post("/review/provider-patterns", response_model=JobOut,
             status_code=status.HTTP_201_CREATED)
async def run_provider_pattern_triage(
    min_group_size: int = 3,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JobOut:
    """Group provider/contact-name extractions into pattern candidates.

    Mirrors the medication-pattern flow but on `fact_type='provider_
    relationship'`. Same Accept (suppresses members) / Dismiss
    semantics via patch_candidate_disposition.
    """
    from ..llm.provider_triage import triage_provider_patterns
    job = await triage_provider_patterns(
        db, user, min_group_size=min_group_size,
    )
    candidates = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.job_id == job.id)
        .order_by(SensemakingCandidate.created_at.asc())
    )).scalars().all())
    return _job_to_out(job, candidates)


class PatternSuppressionStats(BaseModel):
    accepted_patterns: int
    suppressed_member_facts: int
    last_accepted_at: datetime | None = None


@router.get("/review/pattern-stats", response_model=PatternSuppressionStats)
async def pattern_suppression_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> PatternSuppressionStats:
    """Aggregate view of how much review burden patterns have absorbed.

    Counts (accepted_patterns) the medication_pattern / provider_pattern
    candidates this user has accepted, and the total number of member
    facts those acceptances suppressed (sum of array_length(fact_ids)).
    Surfaced on the Review Inbox header so the user can SEE that
    pattern compression is actually doing work.
    """
    from sqlalchemy import func as _func
    rows = (await db.execute(
        select(
            _func.count(SensemakingCandidate.id),
            _func.coalesce(
                _func.sum(_func.coalesce(
                    _func.array_length(SensemakingCandidate.fact_ids, 1), 0,
                )),
                0,
            ),
            _func.max(SensemakingCandidate.disposition_at),
        )
        .where(SensemakingCandidate.user_id == user.id)
        .where(SensemakingCandidate.disposition == "accepted")
        .where(SensemakingCandidate.candidate_type.in_(
            ("medication_pattern", "provider_pattern"),
        ))
    )).one()
    return PatternSuppressionStats(
        accepted_patterns=int(rows[0] or 0),
        suppressed_member_facts=int(rows[1] or 0),
        last_accepted_at=rows[2],
    )


@router.post("/sources/{source_id}/sensemake", response_model=JobOut,
             status_code=status.HTTP_201_CREATED)
async def run_source_sensemake(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JobOut:
    try:
        job = await summarize_source(db, user, source_id)
    except SensemakingError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.warning("sensemake_failed", source_id=str(source_id),
                    error=f"{type(e).__name__}: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Sensemaking job failed") from e

    candidates = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.job_id == job.id)
        .order_by(SensemakingCandidate.created_at.asc())
    )).scalars().all())
    return _job_to_out(job, candidates)


@router.get("/sensemaking/jobs/{job_id}", response_model=JobOut)
async def get_sensemaking_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> JobOut:
    job = await db.get(SensemakingJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    candidates = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.job_id == job.id)
        .order_by(SensemakingCandidate.created_at.asc())
    )).scalars().all())
    return _job_to_out(job, candidates)


@router.get("/sources/{source_id}/candidates", response_model=list[CandidateOut])
async def list_source_candidates(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[CandidateOut]:
    """Pending candidates whose `source_ids` array contains this source.

    Returns most recent first so the source page renders the freshest
    sensemaking draft.
    """
    rows = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.user_id == user.id)
        .where(SensemakingCandidate.source_ids.op("&&")([source_id]))
        .where(SensemakingCandidate.disposition.in_(("pending", "accepted", "edited")))
        .order_by(SensemakingCandidate.created_at.desc())
    )).scalars().all())
    return [_candidate_to_out(c) for c in rows]


class DispositionRequest(BaseModel):
    disposition: str  # accepted | edited | dismissed | rejected
    user_edit: str | None = None


@router.patch("/sensemaking/candidates/{candidate_id}", response_model=CandidateOut)
async def patch_candidate_disposition(
    candidate_id: uuid.UUID,
    body: DispositionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> CandidateOut:
    cand = await db.get(SensemakingCandidate, candidate_id)
    if cand is None or cand.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if body.disposition not in {"accepted", "edited", "dismissed", "rejected"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid disposition")

    from datetime import timezone as _tz
    prev = cand.disposition
    cand.disposition = body.disposition
    cand.disposition_at = datetime.now(_tz.utc)
    if body.user_edit is not None:
        cand.user_edit = body.user_edit

    # 2026-05-15 (Nick's pattern-semantics correction): accepting a
    # medication_pattern / provider_pattern now flips member facts to
    # `pattern_managed`, NOT `deferred`. The two states differ:
    #
    #   - `deferred` = hidden everywhere downstream. Used for facts the
    #     user actively wants out of sight (rejected duplicates,
    #     scaffolding noise). Excluded from the global retrieval
    #     `_HIDDEN_STATES` list, so Ask / EI / Timeline / Dossiers
    #     can't see them.
    #   - `pattern_managed` = "user already triaged this pattern."
    #     OUT of the Review Inbox (Inbox queries `review_state =
    #     'needs_review'` explicitly) but PRESENT in every retrieval
    #     surface. Adherence analysis, before/after comparisons,
    #     Event Intelligence, and timeline rendering all see them.
    #
    # The fix: pattern accept compresses the review-inbox decision
    # without losing the underlying signal. A chronic Celebrex daily
    # log is 46 review-inbox decisions worth compressing to one — but
    # the 46 entries are still the adherence record we want to query.
    #
    # Audit lineage still recorded via `pattern_managed_suppression`
    # AuditEvent (kept the event_type for backfill traceability).
    suppressed_count = 0
    _PATTERN_TYPES = ("medication_pattern", "provider_pattern")
    if (
        body.disposition == "accepted"
        and cand.candidate_type in _PATTERN_TYPES
        and cand.fact_ids
    ):
        from ..models.extracted_fact import ExtractedFact
        from sqlalchemy import select as _select
        members = list((await db.execute(
            _select(ExtractedFact)
            .where(ExtractedFact.id.in_(cand.fact_ids))
        )).scalars().all())
        for m in members:
            if m.review_state in ("needs_review", "confirmed"):
                m.review_state = "pattern_managed"
                suppressed_count += 1

    db.add(AuditEvent(
        user_id=user.id,
        event_type="candidate_disposition",
        subject_type="sensemaking_candidate",
        subject_id=str(candidate_id),
        detail={
            "from": prev,
            "to": body.disposition,
            "candidate_type": cand.candidate_type,
            "edited": body.user_edit is not None,
            "suppressed_member_facts": suppressed_count,
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    if suppressed_count:
        db.add(AuditEvent(
            user_id=user.id,
            event_type="pattern_managed_suppression",
            subject_type="sensemaking_candidate",
            subject_id=str(candidate_id),
            detail={
                "fact_count": suppressed_count,
                "pattern_key": (cand.payload or {}).get("pattern_key"),
            },
        ))
    await db.commit()
    return _candidate_to_out(cand)
