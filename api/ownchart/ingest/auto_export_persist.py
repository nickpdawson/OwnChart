"""Shared persistence for parsed Auto-Export facts.

`parse_health_auto_export` (ingest/auto_export.py) returns
`AutoExportFact` dataclasses that carry per-fact overrides — the
`extraction_method`, `review_state`, `anchor_type`, `equivalence_key`,
`client_sample_key`, and Review-Inbox `why_*` fields the medication /
symptom / metric emitters set. Two call sites persist those facts:

  1. `workers/auto_export_job.py` — the async REST-push worker (the
     canonical path).
  2. `routes/sources.py::upload_auto_export` — the legacy Settings UI
     file-upload path.

Before this module existed, (2) hand-rolled its own INSERT loop that
hardcoded `review_state="confirmed"`, `extraction_method=
"health_auto_export"`, and wrote NO `client_sample_key` /
`equivalence_key` — bypassing every override. Re-uploading the same
JSON duplicated every medication dose and marked Skipped doses
`confirmed`. Both call sites now build rows through
`build_anchor_and_fact` so the parser's overrides are the single
source of truth.

Dedup is per-record (migration 0048): the cross-push existing-key
lookup is scoped by `person_record_id` so two records that log the
same medication at the same time don't cross-dedup.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from .auto_export import AutoExportFact


def build_anchor_and_fact(
    f: AutoExportFact,
    *,
    source_id: uuid.UUID,
    person_record_id: uuid.UUID | None,
) -> tuple[EvidenceAnchor, ExtractedFact]:
    """Build the (anchor, fact) pair for one parsed Auto-Export fact,
    honoring every per-fact override.

    The fact's `evidence_anchor_ids` is left empty — the caller is
    expected to `db.flush()` the anchor to get its id, then set
    `fact.evidence_anchor_ids = [anchor.id]` before adding the fact.
    """
    anchor = EvidenceAnchor(
        source_document_id=source_id,
        person_record_id=person_record_id,
        # Default anchor_type is "auto_export_metric" for the vitals /
        # activity / sleep path; medications and symptoms set their own
        # (e.g. "auto_export_medication").
        anchor_type=f.anchor_type or "auto_export_metric",
        section_path=";".join(
            f"{k}={','.join(v)}" for k, v in (f.coded_concepts or {}).items()
        ) or None,
        text_excerpt=f.label,
    )
    fact = ExtractedFact(
        fact_type=f.fact_type,
        person_record_id=person_record_id,
        label=f.label,
        description=f.description,
        date_start=f.date_start,
        date_end=f.date_end,
        date_precision="day",
        coded_concepts=f.coded_concepts or None,
        confidence=f.confidence,
        # Default "confirmed" for passive sensor metrics; medications
        # override to "needs_review" for Skipped doses.
        review_state=f.review_state or "confirmed",
        evidence_anchor_ids=[],
        # Default "health_auto_export" (wearable lane); medications /
        # symptoms override to "patient_self_report" (clinical lane).
        extraction_method=f.extraction_method or "health_auto_export",
        equivalence_key=f.equivalence_key,
        why_needs_review_code=f.why_needs_review_code,
        why_needs_review_text=f.why_needs_review_text,
        review_task_type=f.review_task_type,
        source_context_only_eligible=f.source_context_only_eligible,
        # Idempotency: the per-record partial unique index (migration
        # 0048) drops within-record replays.
        client_sample_key=f.client_sample_key,
    )
    return anchor, fact


async def existing_client_sample_keys(
    db: AsyncSession,
    keys: Iterable[str],
    *,
    person_record_id: uuid.UUID | None,
) -> set[str]:
    """Return the subset of `keys` that already exist ON THIS RECORD.

    Per-record scoping (migration 0048): the Auto-Export iOS app
    re-pushes the full medication history on every push, so we drop
    keys already stored for the same record. Scoping by
    `person_record_id` keeps two records that log the same dose at the
    same second from cross-deduping each other's facts.
    """
    key_list = [k for k in keys if k]
    if not key_list:
        return set()
    rows = await db.execute(
        select(ExtractedFact.client_sample_key)
        .where(ExtractedFact.person_record_id == person_record_id)
        .where(ExtractedFact.client_sample_key.in_(key_list))
    )
    return set(rows.scalars().all())
