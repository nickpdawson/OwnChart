"""LLM-assisted candidate label translation (docs/07 R5).

Translates SNOMED/CPT/ICD-shaped clinical labels into short
patient-readable phrases that the UI prefers when present. Never
overwrites the original `label` — writes to `display_label` with
`display_label_method='llm_v1'`.

Cost guardrails (per Nick, 2026-05-10):
  - Caller passes `limit` to cap rows-per-run.
  - Re-runs skip rows where `display_label IS NOT NULL` (idempotent).
  - Each call writes a `ModelRun` audit row via `call_with_tool`,
    so usage is observable + budgetable from existing infra.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.extracted_fact import ExtractedFact
from ..models.user import User
from .anthropic_client import call_with_tool
from .prompts import get_registry

log = get_logger("ownchart.llm.relabel")


# Fact types where SNOMED-shaped jargon is most common and the
# patient-readability gap is widest. Other types (observation,
# life_context_event) usually have plain labels already.
_RELABEL_FACT_TYPES: tuple[str, ...] = (
    "procedure",
    "condition",
    "medication",
    "encounter",
    "lab_result",
    "imaging_study",
)


async def relabel_one(
    db: AsyncSession,
    user: User,
    fact: ExtractedFact,
) -> str | None:
    """Return the candidate display label for one fact, or None when
    the LLM declined (output was "(no candidate)") or call errored.

    Caller is responsible for storing the result and committing.
    """
    prompt = get_registry().get("relabel_clinical")
    description_clause = (
        f" — description: {fact.description}" if fact.description else ""
    )
    result = await call_with_tool(
        db,
        user,
        prompt,
        user_vars={
            "fact_type": fact.fact_type,
            "label": fact.label,
            "description_clause": description_clause,
        },
        purpose="relabel_clinical",
        input_source_ids=[],
        tool_name=None,  # plain text response, no tool envelope
        max_tokens=200,  # short output — keeps cost minimal
    )
    if result.error:
        log.warning("relabel_failed", fact_id=str(fact.id), error=result.error)
        return None
    text = (result.raw_text or "").strip()
    if not text:
        return None
    # Strip surrounding quotes if the model added them despite the
    # instruction. Be tolerant of one final period.
    text = text.strip('"').strip("'").rstrip(".").strip()
    if not text or text.lower() == "(no candidate)":
        return None
    # Defensive cap — schema is VARCHAR(512), and the prompt asks for
    # ~80 chars but Claude sometimes elaborates.
    return text[:512]


async def relabel_pending(
    db: AsyncSession,
    user: User,
    *,
    limit: int,
    fact_types: Iterable[str] | None = None,
    person_record_id: uuid.UUID | None = None,
) -> dict:
    """Backfill candidate display_labels for up to `limit` facts that
    don't have one yet. Idempotent on re-run (skips rows where
    display_label IS NOT NULL).

    M02 perimeter (Batch 3): when `person_record_id` is supplied,
    scope the SELECT to that record so a caregiver triggering
    relabel against record A does not pay tokens for record B's
    rows. Argument is keyword-only and defaults to None so any
    legacy in-process caller (worker, script) keeps working.

    Returns a summary dict: `{checked, relabeled, declined, errored}`.
    """
    target_types = tuple(fact_types) if fact_types else _RELABEL_FACT_TYPES
    base = (
        select(ExtractedFact)
        .where(ExtractedFact.display_label.is_(None))
        .where(ExtractedFact.fact_type.in_(target_types))
    )
    if person_record_id is not None:
        base = base.where(ExtractedFact.person_record_id == person_record_id)
    rows = list((await db.execute(
        base
        # Skip rows that already look patient-readable (no caps-only
        # ALL-CAPS jargon, no FHIR resource-id fallback). A label
        # with at least one lower-case run of 4+ chars is probably
        # readable enough; we don't waste LLM tokens on those.
        .where(ExtractedFact.label.op("!~")(r"^[A-Z0-9 /,\.\-]+$"))
        .where(~ExtractedFact.label.op("~")(
            r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
            r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
            r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
        ))
        .where(ExtractedFact.review_state.notin_(("deferred", "rejected", "source_only")))
        .order_by(ExtractedFact.created_at.desc())
        .limit(limit)
    )).scalars().all())

    # The pre-filter above is conservative — it tries to NOT translate
    # already-readable labels. For the backfill case we also want to
    # catch ALL-CAPS rows the filter rejected; pull a second tranche
    # if the first came up short.
    if len(rows) < limit:
        extra_base = (
            select(ExtractedFact)
            .where(ExtractedFact.display_label.is_(None))
            .where(ExtractedFact.fact_type.in_(target_types))
        )
        if person_record_id is not None:
            extra_base = extra_base.where(
                ExtractedFact.person_record_id == person_record_id,
            )
        extra = list((await db.execute(
            extra_base
            .where(ExtractedFact.label.op("~")(r"^[A-Z0-9 /,\.\-]{8,}$"))
            .where(~ExtractedFact.label.op("~")(
                r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
                r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
                r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
            ))
            .where(ExtractedFact.review_state.notin_(("deferred", "rejected", "source_only")))
            .order_by(ExtractedFact.created_at.desc())
            .limit(limit - len(rows))
        )).scalars().all())
        rows.extend(extra)

    relabeled = 0
    declined = 0
    errored = 0
    for fact in rows:
        try:
            candidate = await relabel_one(db, user, fact)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "relabel_exception",
                fact_id=str(fact.id), error=f"{type(e).__name__}: {e}",
            )
            errored += 1
            continue
        if candidate is None:
            declined += 1
            continue
        fact.display_label = candidate
        fact.display_label_method = "llm_v1"
        relabeled += 1
        # Commit per-fact so a mid-run failure doesn't lose prior work.
        await db.commit()

    log.info(
        "relabel_backfill_done",
        checked=len(rows),
        relabeled=relabeled,
        declined=declined,
        errored=errored,
        ran_at=datetime.now(timezone.utc).isoformat(),
    )
    return {
        "checked": len(rows),
        "relabeled": relabeled,
        "declined": declined,
        "errored": errored,
    }
