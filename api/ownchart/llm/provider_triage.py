"""Provider / contact noise triage — Review Inbox compression
(docs/07 §398, docs/08 Review Queue Triage).

Fax cover sheets, scheduling clerks, records custodians, and similar
provider/contact extracts produce hundreds of one-row review chores
that almost never change the patient's story. This triage groups
them by normalized name + role into `provider_pattern` candidate
rows so the Review Inbox shows one decision per pattern.

Mirrors `llm/medication_triage.py` structurally; same SensemakingJob
+ SensemakingCandidate shape so the existing accept-suppresses-members
plumbing in routes/sensemaking.py:patch_candidate_disposition works
unchanged.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.audit_event import AuditEvent
from ..models.extracted_fact import ExtractedFact
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.sensemaking_job import SensemakingJob
from ..models.user import User

log = get_logger("ownchart.llm.provider_triage")


_TITLE_RE = re.compile(
    r"\b(md|do|np|pa|rn|lpn|md\.|do\.|np\.|pa\.|rn\.|"
    r"physician|nurse|provider|coordinator|scheduler|"
    r"records?\s+custodian|utilization\s+management)\b",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _normalize_provider_label(label: str) -> str:
    """Strip credentials / role words so 'Dr. Jane Smith, MD' groups
    with 'Jane Smith RN' (loosely). Aggressive on purpose — false
    grouping is fine because every member fact is still individually
    reviewable. Tight grouping is worse than loose for V1.
    """
    s = (label or "").lower().strip()
    # Drop "Dr." prefixes and trailing credentials.
    s = re.sub(r"^(dr\.?|mr\.?|mrs\.?|ms\.?|miss)\s+", "", s)
    s = _TITLE_RE.sub("", s)
    # Strip punctuation, collapse whitespace.
    s = re.sub(r"[.,;:()\[\]]", " ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


async def triage_provider_patterns(
    db: AsyncSession,
    user: User,
    *,
    min_group_size: int = 3,
    limit_rows: int = 5000,
    person_record_id: uuid.UUID | None = None,
) -> SensemakingJob:
    """Walk provider_relationship facts in review_state ∈
    {needs_review, confirmed} and emit pattern candidates for any
    normalized-label group with ≥ `min_group_size` rows.

    `min_group_size=3` is intentionally lower than the medication
    triage default (5) — provider name noise repeats less than
    medication dosing, but 3+ shared names from different sources
    still benefits from one decision instead of three.

    M02 perimeter (Batch 9): `person_record_id` scopes the fact
    walk + stamps every persisted row (job, candidates, audit event)
    so triage results live on the active record only. Defaults to
    None for legacy in-process callers.
    """
    now = datetime.now(timezone.utc)
    job = SensemakingJob(
        user_id=user.id,
        person_record_id=person_record_id,
        job_type="provider_pattern_triage",
        status="running",
        privacy_mode="off",  # deterministic — no PHI off host
        scope={"min_group_size": min_group_size},
        started_at=now,
    )
    db.add(job)
    await db.flush()

    fact_stmt = (
        select(ExtractedFact)
        .where(ExtractedFact.fact_type == "provider_relationship")
        .where(ExtractedFact.review_state.in_(("needs_review", "confirmed")))
        .where(ExtractedFact.significance != "source_only")
        .order_by(ExtractedFact.created_at.desc())
        .limit(limit_rows)
    )
    if person_record_id is not None:
        fact_stmt = fact_stmt.where(
            ExtractedFact.person_record_id == person_record_id
        )
    rows = list((await db.execute(fact_stmt)).scalars().all())

    groups: dict[str, dict[str, Any]] = {}
    for f in rows:
        key = _normalize_provider_label(f.label or "")
        if not key:
            continue
        g = groups.setdefault(key, {
            "key": key,
            "label_examples": set(),
            "fact_ids": [],
            "needs_review_count": 0,
        })
        g["label_examples"].add((f.display_label or f.label) or key)
        g["fact_ids"].append(f.id)
        if f.review_state == "needs_review":
            g["needs_review_count"] += 1

    # Skip groups whose key is already in a pending candidate.
    existing = (await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.user_id == user.id)
        .where(SensemakingCandidate.candidate_type == "provider_pattern")
        .where(SensemakingCandidate.disposition == "pending")
    )).scalars().all()
    existing_keys = {
        (c.payload or {}).get("pattern_key") for c in existing
        if isinstance(c.payload, dict)
    }

    created = 0
    for key, g in groups.items():
        if len(g["fact_ids"]) < min_group_size:
            continue
        if key in existing_keys:
            continue
        label_examples = sorted(g["label_examples"])[:3]
        title = f"{label_examples[0]} (provider/contact pattern)"
        bits: list[str] = [f"{len(g['fact_ids'])} entries"]
        if g["needs_review_count"]:
            bits.append(f"{g['needs_review_count']} in review")
        summary = " · ".join(bits)
        candidate = SensemakingCandidate(
            user_id=user.id,
            person_record_id=person_record_id,
            job_id=job.id,
            candidate_type="provider_pattern",
            title=title,
            summary_text=summary,
            payload={
                "pattern_key": key,
                "label_examples": label_examples,
                "needs_review_count": g["needs_review_count"],
            },
            claim_label="statistical",
            fact_ids=list(g["fact_ids"])[:500],
            disposition="pending",
        )
        db.add(candidate)
        created += 1

    job.status = "completed"
    job.completed_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=person_record_id,
        event_type="provider_pattern_triage_completed",
        subject_type="sensemaking_job",
        subject_id=str(job.id),
        detail={"groups_seen": len(groups), "patterns_created": created},
    ))
    await db.commit()
    log.info("provider_pattern_triage", groups=len(groups), created=created)
    return job
