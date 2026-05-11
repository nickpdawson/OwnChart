"""Medication-pattern triage — Review Inbox compression (docs/08 §475).

The Review Inbox should never show 200 individual "you logged this
dose of Creatine as Skipped" rows. The pattern triage groups all
medication facts (and any fact_type='symptom' patient-log entries)
that share a normalized label into pattern candidates so the user
sees one decision per pattern, not 200.

V1 is fully deterministic — no LLM is required. The output lives in
`sensemaking_candidates` with `candidate_type='medication_pattern'` so
it slots into the existing candidate UI surface.
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

log = get_logger("ownchart.llm.medication_triage")


_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g|ml|iu|units?)\b",
    re.IGNORECASE,
)


def _normalize_med_label(label: str) -> str:
    """Strip dose / route / units so we can group "Creatine 5g" with
    "Creatine 10g" and "Creatine" into one pattern."""
    s = (label or "").strip().lower()
    s = _DOSE_RE.sub("", s)
    s = re.sub(r"\s*\b(oral|tablet|capsule|cream|gel|patch|injection|po|im|iv|subcut|topical)\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def triage_medication_patterns(
    db: AsyncSession,
    user: User,
    *,
    min_group_size: int = 5,
    limit_rows: int = 5000,
) -> SensemakingJob:
    """Walk medication + symptom facts in `review_state ∈ {needs_review,
    confirmed}` and emit pattern candidates for any normalized-label
    group with ≥ `min_group_size` rows.

    Idempotent: re-running creates a fresh job, but existing pending
    candidates with matching `pattern_key` payload are left in place
    (we only add new ones).
    """
    now = datetime.now(timezone.utc)
    job = SensemakingJob(
        user_id=user.id,
        job_type="medication_pattern_triage",
        status="running",
        privacy_mode="off",  # deterministic — no PHI leaves the host
        scope={"min_group_size": min_group_size},
        started_at=now,
    )
    db.add(job)
    await db.flush()

    rows = list((await db.execute(
        select(ExtractedFact)
        .where(ExtractedFact.fact_type.in_(("medication", "symptom")))
        .where(ExtractedFact.review_state.in_(("needs_review", "confirmed")))
        .where(ExtractedFact.significance != "source_only")
        .order_by(ExtractedFact.date_start.desc().nullslast(), ExtractedFact.created_at.desc())
        .limit(limit_rows)
    )).scalars().all())

    groups: dict[str, dict[str, Any]] = {}
    for f in rows:
        key = _normalize_med_label(f.label or "")
        if not key:
            continue
        g = groups.setdefault(key, {
            "key": key,
            "label_examples": set(),
            "fact_ids": [],
            "skipped_count": 0,
            "taken_count": 0,
            "needs_review_count": 0,
            "date_min": None,
            "date_max": None,
            "fact_type": f.fact_type,
        })
        g["label_examples"].add((f.display_label or f.label) or key)
        g["fact_ids"].append(f.id)
        if f.review_state == "needs_review":
            g["needs_review_count"] += 1
        if f.description and "Skipped" in f.description:
            g["skipped_count"] += 1
        elif f.description and "Taken" in f.description:
            g["taken_count"] += 1
        if f.date_start is not None:
            if g["date_min"] is None or f.date_start < g["date_min"]:
                g["date_min"] = f.date_start
            if g["date_max"] is None or f.date_start > g["date_max"]:
                g["date_max"] = f.date_start

    # Existing pending candidates for this user/job_type — skip dup keys.
    existing = (await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.user_id == user.id)
        .where(SensemakingCandidate.candidate_type == "medication_pattern")
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
        title = f"{label_examples[0]} ({g['fact_type']} pattern)"
        bits: list[str] = [f"{len(g['fact_ids'])} entries"]
        if g["skipped_count"]:
            bits.append(f"{g['skipped_count']} skipped")
        if g["taken_count"]:
            bits.append(f"{g['taken_count']} taken")
        if g["needs_review_count"]:
            bits.append(f"{g['needs_review_count']} in review")
        if g["date_min"] and g["date_max"] and g["date_min"] != g["date_max"]:
            bits.append(
                f"{g['date_min'].date().isoformat()}→{g['date_max'].date().isoformat()}"
            )
        summary = " · ".join(bits)

        candidate = SensemakingCandidate(
            user_id=user.id,
            job_id=job.id,
            candidate_type="medication_pattern",
            title=title,
            summary_text=summary,
            payload={
                "pattern_key": key,
                "label_examples": label_examples,
                "skipped_count": g["skipped_count"],
                "taken_count": g["taken_count"],
                "needs_review_count": g["needs_review_count"],
                "date_min": g["date_min"].isoformat() if g["date_min"] else None,
                "date_max": g["date_max"].isoformat() if g["date_max"] else None,
                "fact_type": g["fact_type"],
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
        event_type="medication_pattern_triage_completed",
        subject_type="sensemaking_job",
        subject_id=str(job.id),
        detail={"groups_seen": len(groups), "patterns_created": created},
    ))
    await db.commit()
    log.info("medication_pattern_triage", groups=len(groups), created=created)
    return job
