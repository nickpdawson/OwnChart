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
    person_record_id: uuid.UUID | None = None,
) -> SensemakingJob:
    """Walk medication + symptom facts in `review_state ∈ {needs_review,
    confirmed}` and emit pattern candidates for any normalized-label
    group with ≥ `min_group_size` rows.

    Idempotent: re-running creates a fresh job, but existing pending
    candidates with matching `pattern_key` payload are left in place
    (we only add new ones).

    M02 perimeter (Batch 9): `person_record_id` scopes the fact
    walk + stamps every persisted row (job, candidates, audit event)
    so triage results live on the active record only. Defaults to
    None for legacy in-process callers (workers, scripts); the route
    layer always passes ctx.active_record_id.
    """
    now = datetime.now(timezone.utc)
    job = SensemakingJob(
        user_id=user.id,
        person_record_id=person_record_id,
        job_type="medication_pattern_triage",
        status="running",
        privacy_mode="off",  # deterministic — no PHI leaves the host
        scope={"min_group_size": min_group_size},
        started_at=now,
    )
    db.add(job)
    await db.flush()

    # Only un-triaged facts feed new patterns. `pattern_managed` rows
    # are already "user accepted this pattern" — don't re-suggest the
    # same grouping at every triage run.
    fact_stmt = (
        select(ExtractedFact)
        .where(ExtractedFact.fact_type.in_(("medication", "symptom")))
        .where(ExtractedFact.review_state.in_(("needs_review", "confirmed")))
        .where(ExtractedFact.significance != "source_only")
        .order_by(ExtractedFact.date_start.desc().nullslast(), ExtractedFact.created_at.desc())
        .limit(limit_rows)
    )
    if person_record_id is not None:
        fact_stmt = fact_stmt.where(
            ExtractedFact.person_record_id == person_record_id
        )
    rows = list((await db.execute(fact_stmt)).scalars().all())

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
            # 2026-05-15 (richer signal — Nick's correction): preserve
            # per-occurrence data the user expects to query later
            # (adherence, dose distribution, active days).
            "active_dates": set(),     # distinct date_start.date()
            "dose_examples": set(),    # raw label dose strings
        })
        g["label_examples"].add((f.display_label or f.label) or key)
        g["fact_ids"].append(f.id)
        if f.review_state == "needs_review":
            g["needs_review_count"] += 1
        # Adherence classification — keep three buckets explicit so the
        # UI can show 'taken / skipped / unknown'. Anything that isn't
        # tagged Taken or Skipped in the description is "unknown" — log
        # entry exists but the adherence flag wasn't captured.
        desc = f.description or ""
        if "Skipped" in desc:
            g["skipped_count"] += 1
        elif "Taken" in desc:
            g["taken_count"] += 1
        # else: unknown — derived as len(fact_ids) - taken - skipped
        if f.date_start is not None:
            if g["date_min"] is None or f.date_start < g["date_min"]:
                g["date_min"] = f.date_start
            if g["date_max"] is None or f.date_start > g["date_max"]:
                g["date_max"] = f.date_start
            g["active_dates"].add(f.date_start.date())
        # Dose extraction — pull each "20mg / 200mg" token off the
        # original label so the user can see "20mg | 200mg" if they
        # have multiple strengths logged for the same med.
        for dose_match in _DOSE_RE.findall(f.label or ""):
            g["dose_examples"].add(dose_match.lower().strip())

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

        # Richer signal payload (Nick's 2026-05-15 correction):
        # active_days, entries/active month, distinct doses, clustered
        # use. The card stops being "Accept to hide" and starts being
        # "here's what your record shows about this med, and you can
        # mark it reviewed in one click."
        total_entries = len(g["fact_ids"])
        active_days = len(g["active_dates"])
        unknown_count = total_entries - g["taken_count"] - g["skipped_count"]
        dose_examples = sorted(g["dose_examples"])[:6]

        # Active-month math: count distinct (year, month) pairs across
        # active_dates. Divide total entries by that to get a per-active-
        # month average. "Active" because chronic meds may have months of
        # non-use; entries/total-month would understate adherence on
        # those.
        active_months: set[tuple[int, int]] = set()
        for d in g["active_dates"]:
            active_months.add((d.year, d.month))
        entries_per_active_month = (
            round(total_entries / len(active_months), 1)
            if active_months else None
        )

        # Clustered-use heuristic — if 60%+ of entries fall within
        # 20% of the date range, flag it. Catches "I was on this for
        # one stretch in 2024 then never again" so the user has a
        # chance to interpret before marking reviewed.
        clustered_window: dict[str, Any] | None = None
        if g["date_min"] and g["date_max"] and active_days >= 5:
            span_days = max(1, (g["date_max"] - g["date_min"]).days)
            if span_days >= 30:
                # Sliding window: find the densest 20% of the range.
                sorted_dates = sorted(g["active_dates"])
                window_days = max(1, int(span_days * 0.20))
                best_count = 0
                best_start = sorted_dates[0]
                best_end = sorted_dates[0]
                j = 0
                for i in range(len(sorted_dates)):
                    while j < len(sorted_dates) and (
                        (sorted_dates[j] - sorted_dates[i]).days <= window_days
                    ):
                        j += 1
                    count = j - i
                    if count > best_count:
                        best_count = count
                        best_start = sorted_dates[i]
                        best_end = sorted_dates[j - 1]
                if best_count / active_days >= 0.60:
                    clustered_window = {
                        "start": best_start.isoformat(),
                        "end": best_end.isoformat(),
                        "days_in_window": best_count,
                        "share_of_active_days": round(best_count / active_days, 2),
                    }

        # New summary copy (Nick's 2026-05-15): explain what's being
        # marked, not hidden. The card now reads like clinical signal,
        # not a delete button.
        most_recent_str = (
            g["date_max"].strftime("%b %-d, %Y") if g["date_max"] else None
        )
        date_range_str = (
            f"from {g['date_min'].strftime('%b %Y')} to {g['date_max'].strftime('%b %Y')}"
            if g["date_min"] and g["date_max"] and g["date_min"] != g["date_max"]
            else (g["date_min"].strftime("%b %Y") if g["date_min"] else "undated")
        )
        adherence_parts: list[str] = []
        if g["taken_count"]:
            adherence_parts.append(f"{g['taken_count']} taken")
        if g["skipped_count"]:
            adherence_parts.append(f"{g['skipped_count']} skipped")
        if unknown_count > 0:
            adherence_parts.append(f"{unknown_count} unknown")
        adherence_str = ", ".join(adherence_parts) if adherence_parts else None

        # Single-line headline summary for callers that only render
        # summary_text (older surfaces, audit logs, etc.). The Review
        # UI reads the richer payload directly.
        summary_bits = [f"{total_entries} log entries {date_range_str}"]
        if adherence_str:
            summary_bits.append(adherence_str)
        if active_days:
            summary_bits.append(f"used on {active_days} active days")
        if most_recent_str:
            summary_bits.append(f"most recent {most_recent_str}")
        summary = " · ".join(summary_bits)

        candidate = SensemakingCandidate(
            user_id=user.id,
            person_record_id=person_record_id,
            job_id=job.id,
            candidate_type="medication_pattern",
            title=title,
            summary_text=summary,
            payload={
                "pattern_key": key,
                "label_examples": label_examples,
                "fact_type": g["fact_type"],
                # Counts
                "total_entries": total_entries,
                "skipped_count": g["skipped_count"],
                "taken_count": g["taken_count"],
                "unknown_count": unknown_count,
                "needs_review_count": g["needs_review_count"],
                # Time
                "date_min": g["date_min"].isoformat() if g["date_min"] else None,
                "date_max": g["date_max"].isoformat() if g["date_max"] else None,
                "active_days": active_days,
                "active_months": len(active_months),
                "entries_per_active_month": entries_per_active_month,
                # Dose / cluster
                "dose_examples": dose_examples,
                "clustered_window": clustered_window,
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
        event_type="medication_pattern_triage_completed",
        subject_type="sensemaking_job",
        subject_id=str(job.id),
        detail={"groups_seen": len(groups), "patterns_created": created},
    ))
    await db.commit()
    log.info("medication_pattern_triage", groups=len(groups), created=created)
    return job
