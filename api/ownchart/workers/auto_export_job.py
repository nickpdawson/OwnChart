"""Background processing for Health Auto Export pushes.

The push endpoint can't process the JSON synchronously: an 8 MB push
with ~40k facts takes ~15 seconds of upload + parsing + DB writes,
which is past the iOS Health Auto Export app's request timeout. The
app gives up client-side, the server keeps going, the facts persist,
the app retries the same payload — duplicate ingest forever.

Fix: the push endpoint writes the raw bytes to storage, creates a
SourceDocument with `processing_status='pending'`, enqueues this
worker, and returns 202 immediately. The worker reads the saved
JSON, parses, and writes facts in batches.

The Arq task name is `process_auto_export_push` and is registered
on the same WorkerSettings as vision extraction so a single worker
container handles both.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..ingest import auto_export as auto_export_ingest
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument

log = get_logger("ownchart.workers.auto_export")


async def process_auto_export_push(ctx: dict[str, Any], source_id: str) -> dict[str, Any]:
    """Parse a saved Auto Export push and create facts.

    Idempotent: if `processing_status` is already 'completed' or
    'failed', returns immediately without re-processing. Reads the raw
    JSON from `storage_uri`; never re-fetches from the iOS app.
    """
    src_uuid = uuid.UUID(source_id)
    async with SessionLocal() as db:
        src = await db.get(SourceDocument, src_uuid)
        if src is None:
            log.warning("auto_export_source_missing", source_id=source_id)
            return {"error": "source missing"}
        meta = dict(src.raw_metadata or {})
        if meta.get("processing_status") in ("completed", "failed"):
            log.info("auto_export_already_processed", source_id=source_id, status=meta.get("processing_status"))
            return {"status": meta.get("processing_status")}

        meta["processing_status"] = "running"
        meta["processing_started_at"] = datetime.now(timezone.utc).isoformat()
        src.raw_metadata = meta
        await db.commit()

    # Load + parse outside the DB session to keep the transaction short.
    bundle_path = Path(src.storage_uri)
    if not bundle_path.exists():
        async with SessionLocal() as db:
            row = await db.get(SourceDocument, src_uuid)
            if row is not None:
                m = dict(row.raw_metadata or {})
                m["processing_status"] = "failed"
                m["processing_error"] = f"bundle missing on disk: {bundle_path}"
                m["processing_completed_at"] = datetime.now(timezone.utc).isoformat()
                row.raw_metadata = m
                await db.commit()
        log.warning("auto_export_bundle_missing", source_id=source_id, path=str(bundle_path))
        return {"status": "failed", "error": "bundle missing"}

    try:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        async with SessionLocal() as db:
            row = await db.get(SourceDocument, src_uuid)
            if row is not None:
                m = dict(row.raw_metadata or {})
                m["processing_status"] = "failed"
                m["processing_error"] = f"json parse failed: {type(e).__name__}: {e}"
                m["processing_completed_at"] = datetime.now(timezone.utc).isoformat()
                row.raw_metadata = m
                await db.commit()
        log.warning("auto_export_parse_failed", source_id=source_id, error=str(e))
        return {"status": "failed", "error": str(e)}

    parsed = auto_export_ingest.parse_health_auto_export(payload)

    # Write the facts in chunks so a giant push doesn't hold one
    # transaction open forever.
    BATCH = 500
    fact_count = 0
    pending: list[tuple[EvidenceAnchor, ExtractedFact]] = []

    async def _flush() -> None:
        nonlocal fact_count, pending
        if not pending:
            return
        async with SessionLocal() as db:
            for anchor, _fact in pending:
                db.add(anchor)
            await db.flush()
            for anchor, fact in pending:
                fact.evidence_anchor_ids = [anchor.id]
                db.add(fact)
            await db.commit()
        fact_count += len(pending)
        pending = []

    for f in parsed.facts:
        anchor = EvidenceAnchor(
            source_document_id=src_uuid,
            # Default anchor_type is "auto_export_metric" for the
            # vital-signs / activity / sleep path; medications and
            # symptoms set their own (e.g. auto_export_medication).
            anchor_type=f.anchor_type or "auto_export_metric",
            section_path=";".join(
                f"{k}={','.join(v)}" for k, v in (f.coded_concepts or {}).items()
            ) or None,
            text_excerpt=f.label,
        )
        ef = ExtractedFact(
            fact_type=f.fact_type,
            label=f.label,
            description=f.description,
            date_start=f.date_start,
            date_end=f.date_end,
            date_precision="day",
            coded_concepts=f.coded_concepts or None,
            confidence=f.confidence,
            # Default review_state is "confirmed" for Auto Export's
            # passive metrics (heart rate, steps — provider-attested).
            # Medications use "needs_review" for Skipped doses; the
            # parser sets review_state explicitly when it differs.
            review_state=f.review_state or "confirmed",
            evidence_anchor_ids=[],
            # Default extraction_method is "health_auto_export" so
            # the global timeline classifies the fact as wearable.
            # Medications / symptoms override to "patient_self_report"
            # so they land in the clinical lane instead.
            extraction_method=f.extraction_method or "health_auto_export",
            # Cross-source equivalence (docs/07 §487-545). NULL for
            # facts without a canonicalization rule (medications,
            # workouts, sleep, body metrics in V1); same key across
            # sources for daily-aggregate metrics.
            equivalence_key=f.equivalence_key,
            # docs/07 Priority 1 review reasons. Populated for the
            # confidently-classifiable cases (Auto Export Skipped
            # medications); NULL for the rest, which keeps the
            # Review Inbox quiet for items we can't yet explain.
            why_needs_review_code=f.why_needs_review_code,
            why_needs_review_text=f.why_needs_review_text,
            review_task_type=f.review_task_type,
            source_context_only_eligible=f.source_context_only_eligible,
        )
        pending.append((anchor, ef))
        if len(pending) >= BATCH:
            await _flush()
    await _flush()

    async with SessionLocal() as db:
        row = await db.get(SourceDocument, src_uuid)
        if row is not None:
            m = dict(row.raw_metadata or {})
            m["processing_status"] = "completed"
            m["processing_completed_at"] = datetime.now(timezone.utc).isoformat()
            m["metric_counts"] = parsed.metric_counts
            m["workout_count"] = parsed.workout_count
            m["sleep_session_count"] = parsed.sleep_session_count
            m["medication_count"] = parsed.medication_count
            m["symptom_count"] = parsed.symptom_count
            m["fact_count"] = fact_count
            m["skipped_metrics"] = sorted(set(parsed.skipped_metrics))
            m["unhandled_sections"] = sorted(set(parsed.unhandled_sections))
            m["parse_warnings"] = parsed.parse_warnings[:50]
            row.raw_metadata = m
            await db.commit()

    log.info(
        "auto_export_push_processed",
        source_id=source_id,
        fact_count=fact_count,
        workouts=parsed.workout_count,
        sleep_sessions=parsed.sleep_session_count,
        medications=parsed.medication_count,
        symptoms=parsed.symptom_count,
    )
    return {"status": "completed", "fact_count": fact_count}
