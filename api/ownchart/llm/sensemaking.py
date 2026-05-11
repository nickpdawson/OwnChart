"""Sensemaking job runner (docs/08).

V1 implements one job type end-to-end — `source_summary` — and lays
the shape for the rest. The runner:

  1. Loads the input scope (source + facts + topic linkages).
  2. Honors the user's `ai.privacy_mode` setting.
  3. Renders the prompt, fires `call_with_tool`, writes a ModelRun
     (handled by the underlying client).
  4. Persists one SensemakingJob row + one SensemakingCandidate row
     with disposition=pending so the UI can render it as a draft.

The candidate is never canonical. Promoting it (creating Episode rows,
marking source-only facts, etc.) is a separate explicit step the
user takes from the UI.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.audit_event import AuditEvent
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.sensemaking_job import SensemakingJob
from ..models.source_document import SourceDocument
from ..models.topic import Topic
from ..models.user import User
from ..settings.registry import effective
from .anthropic_client import call_with_tool
from .prompts import get_registry

log = get_logger("ownchart.llm.sensemaking")


_FHIR_ID_LABEL_RE = re.compile(
    r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
    r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
    r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
)


class SensemakingError(RuntimeError):
    pass


def _fact_matches_topic(fact: ExtractedFact, topic: Topic) -> bool:
    """Lightweight topic-membership match — mirrors
    sources._fact_matches_topic. Inlined to keep this module
    self-contained (the LLM service can run from workers later
    without importing route modules)."""
    label = (fact.label or "").lower()
    desc = (fact.description or "").lower()
    for alias in (topic.name, *(topic.aliases or [])):
        if not alias:
            continue
        a = alias.lower()
        if a in label or a in desc:
            return True
    for pat in (topic.label_patterns or []):
        if not pat:
            continue
        try:
            if re.search(pat, fact.label or "", re.IGNORECASE):
                return True
            if re.search(pat, fact.description or "", re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _format_fact_line(f: ExtractedFact) -> str:
    label = f.display_label or f.label or "(no label)"
    date = f.date_start.date().isoformat() if f.date_start else "unknown date"
    return f"{f.id} | {f.fact_type} | {date} | {label}"


async def summarize_source(
    db: AsyncSession,
    user: User,
    source_id: uuid.UUID,
    *,
    sample_size: int = 60,
    excerpt_count: int = 8,
) -> SensemakingJob:
    """Run a `source_summary` job. Returns the SensemakingJob row
    (with `model_run_id` and an attached SensemakingCandidate when
    successful).

    Refuses with `status='refused'` when:
      - `ai.privacy_mode` is `off`.
      - The source has no extracted facts.
    Refused jobs still get persisted so the UI can render the reason.
    """
    privacy_mode = await effective(db, user, "ai.privacy_mode")
    label_xlation = await effective(db, user, "ai.label_translation")

    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise SensemakingError(f"source {source_id} not found")

    job = SensemakingJob(
        user_id=user.id,
        job_type="source_summary",
        status="pending",
        privacy_mode=privacy_mode,
        scope={"source_id": str(source_id)},
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    await db.flush()

    db.add(AuditEvent(
        user_id=user.id,
        event_type="sensemaking_started",
        subject_type="source",
        subject_id=str(source_id),
        detail={"job_id": str(job.id), "privacy_mode": privacy_mode},
    ))

    if privacy_mode == "off":
        job.status = "refused"
        job.error = "ai.privacy_mode is off"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return job

    if not user.phi_consent_granted:
        job.status = "refused"
        job.error = "PHI consent not granted"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return job

    anchor_ids = list((await db.execute(
        select(EvidenceAnchor.id).where(EvidenceAnchor.source_document_id == source_id)
    )).scalars().all())
    if not anchor_ids:
        job.status = "refused"
        job.error = "no extracted facts"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return job

    facts = list((await db.execute(
        select(ExtractedFact).where(
            ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids)
        ).order_by(ExtractedFact.date_start.desc().nullslast())
    )).scalars().all())

    fact_type_counts: dict[str, int] = {}
    dated: list[ExtractedFact] = []
    for f in facts:
        fact_type_counts[f.fact_type] = fact_type_counts.get(f.fact_type, 0) + 1
        if f.date_start is not None:
            dated.append(f)
    date_min = min((f.date_start for f in dated), default=None)
    date_max = max((f.date_start for f in dated), default=None)

    topics = list((await db.execute(select(Topic))).scalars().all())
    topic_lines = []
    for t in topics:
        matched = sum(1 for f in facts if _fact_matches_topic(f, t))
        if matched > 0:
            topic_lines.append((t.name, matched))
    topic_lines.sort(key=lambda x: -x[1])

    # Prefer the most care-meaningful, dated, non-FHIR-ID-labeled facts
    # in the sample. Cap to keep token usage bounded.
    def _care_priority(f: ExtractedFact) -> int:
        if _FHIR_ID_LABEL_RE.match(f.label or ""):
            return 0
        return {
            "procedure": 5, "condition": 4, "encounter": 3,
            "medication": 3, "lab_result": 2, "imaging_study": 2,
            "observation": 1, "symptom": 1,
        }.get(f.fact_type, 1)

    sample = sorted(facts, key=lambda f: (_care_priority(f),
                                          f.date_start.timestamp() if f.date_start else 0),
                    reverse=True)[:sample_size]

    # Privacy-mode gates what we put on the wire.
    #   metadata_only   → fact lines (label + date + type) only; no excerpts.
    #   selected_evidence → fact lines + a small set of evidence excerpts.
    #   full_source     → same as selected_evidence in V1 (no OCR dump yet).
    excerpts: list[str] = []
    if privacy_mode in ("selected_evidence", "full_source"):
        anchor_rows = list((await db.execute(
            select(EvidenceAnchor)
            .where(EvidenceAnchor.id.in_(anchor_ids))
            .limit(excerpt_count)
        )).scalars().all())
        for a in anchor_rows:
            ex = (a.text_excerpt or "").strip()
            if not ex:
                continue
            ex = ex[:280]
            excerpts.append(f"- {ex}")

    if date_min and date_max:
        date_range = f"{date_min.date().isoformat()} to {date_max.date().isoformat()}"
    elif date_min:
        date_range = date_min.date().isoformat()
    else:
        date_range = "unknown"

    prompt = get_registry().get("source_sensemaking")
    result = await call_with_tool(
        db,
        user,
        prompt,
        user_vars={
            "source_name": (src.source_label or src.original_filename or "Untitled source"),
            "source_kind": src.source_type,
            "source_date": src.acquired_at.date().isoformat() if src.acquired_at else "unknown",
            "fact_count": str(len(facts)),
            "date_range": date_range,
            "topic_block": ("\n".join(f"- {n} ({c})" for n, c in topic_lines[:10]) or "(none)"),
            "fact_type_block": ("\n".join(f"- {k}: {v}" for k, v in
                                          sorted(fact_type_counts.items(),
                                                 key=lambda kv: -kv[1])) or "(none)"),
            "sample_size": str(len(sample)),
            "facts_block": ("\n".join(_format_fact_line(f) for f in sample) or "(none)"),
            "excerpt_count": str(len(excerpts)),
            "excerpts_block": ("\n".join(excerpts) if excerpts else "(privacy mode does not include excerpts)"),
        },
        purpose="source_sensemaking",
        input_source_ids=[source_id],
        tool_name="emit_source_sensemaking",
    )

    job.model_run_id = result.model_run_id
    job.completed_at = datetime.now(timezone.utc)

    if result.error or not result.tool_input:
        job.status = "failed"
        job.error = result.error or "no structured tool output"
        await db.commit()
        return job

    payload = result.tool_input

    # Safety refusal short-circuits the candidate.
    if payload.get("safety_response"):
        job.status = "completed"
        await db.commit()
        db.add(SensemakingCandidate(
            user_id=user.id,
            job_id=job.id,
            candidate_type="safety_response",
            title=None,
            summary_text=payload["safety_response"],
            payload={"raw": payload},
            claim_label="unknown",
            source_ids=[source_id],
            disposition="pending",
        ))
        db.add(AuditEvent(
            user_id=user.id,
            event_type="sensemaking_completed",
            subject_type="source",
            subject_id=str(source_id),
            detail={"job_id": str(job.id), "safety_refusal": True},
        ))
        await db.commit()
        return job

    job.status = "completed"

    # Primary candidate — the patient-readable source summary.
    summary_text = (payload.get("summary") or "").strip()
    cand = SensemakingCandidate(
        user_id=user.id,
        job_id=job.id,
        candidate_type="source_summary",
        title=(src.source_label or src.original_filename or "Source summary"),
        summary_text=summary_text,
        payload={
            "source_only_recommendation": payload.get("source_only_recommendation") or "",
            "suggested_questions": payload.get("suggested_questions") or [],
        },
        claim_label=payload.get("claim_label") or "unknown",
        source_ids=[source_id],
        disposition="pending",
    )
    db.add(cand)

    # Episode candidates — secondary rows so the UI can render each
    # one as its own draftable group.
    for ep in (payload.get("episode_candidates") or [])[:3]:
        title = (ep.get("title") or "").strip()
        if not title:
            continue
        fact_id_strs = ep.get("fact_ids") or []
        valid_fact_ids: list[uuid.UUID] = []
        for fid in fact_id_strs:
            try:
                valid_fact_ids.append(uuid.UUID(str(fid)))
            except (TypeError, ValueError):
                continue
        db.add(SensemakingCandidate(
            user_id=user.id,
            job_id=job.id,
            candidate_type="episode",
            title=title,
            summary_text=(ep.get("why") or "").strip(),
            payload={},
            claim_label=payload.get("claim_label") or "inferred",
            source_ids=[source_id],
            fact_ids=valid_fact_ids,
            disposition="pending",
        ))

    db.add(AuditEvent(
        user_id=user.id,
        event_type="sensemaking_completed",
        subject_type="source",
        subject_id=str(source_id),
        detail={
            "job_id": str(job.id),
            "candidates": 1 + len(payload.get("episode_candidates") or []),
            "label_translation_enabled": bool(label_xlation),
        },
    ))
    await db.commit()
    return job
