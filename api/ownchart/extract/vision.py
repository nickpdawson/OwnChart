"""Claude Vision extraction over rendered page images.

Each page is sent as base64 with the `extract_fax_vision.v1` prompt forcing
tool-use output. Every call writes a ModelRun audit row. Resulting facts
are persisted alongside per-page evidence anchors.

This is the EXPENSIVE lane — gated on PHI consent. Caller must hold a
ready user and DB session. Errors per page are isolated: one page failing
doesn't abort the rest.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..ingest.fact_classifier import review_state_for_vision
from ..llm import call_with_tool, get_registry
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User

log = get_logger("ownchart.extract.vision")


_CONFIDENCE_INT = {
    "high": 90,
    "medium": 65,
    "low": 35,
    "possible": 20,
    "unknown": None,
}


@dataclass
class VisionPageResult:
    page_number: int
    model_run_id: uuid.UUID | None
    error: str | None
    fact_count: int


@dataclass
class VisionExtractionSummary:
    source_id: uuid.UUID
    pages: list[VisionPageResult]

    @property
    def total_facts(self) -> int:
        return sum(p.fact_count for p in self.pages)

    @property
    def errors(self) -> list[str]:
        return [p.error for p in self.pages if p.error]


def _media_type_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "image/png")


def _date_from_emit(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    s = date_str.strip()
    # Try ISO 8601 first, then YYYY-MM-DD, then YYYY-MM, then YYYY.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def extract_page(
    db: AsyncSession,
    user: User,
    source: SourceDocument,
    page_number: int,
    image_path: Path,
    page_anchor: EvidenceAnchor,
    patient_context: str | None = None,
) -> VisionPageResult:
    """Run Claude Vision on a single page; persist any emitted facts."""
    if not image_path.exists():
        return VisionPageResult(
            page_number=page_number,
            model_run_id=None,
            error=f"page image missing: {image_path}",
            fact_count=0,
        )

    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    media_type = _media_type_from_path(image_path)

    prompt = get_registry().get("extract_fax_vision")
    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "source_label": source.source_label or source.original_filename or "(unlabeled)",
            "page_number": page_number,
            "patient_context": patient_context or "(none)",
        },
        purpose="extract_fax_vision",
        input_source_ids=[source.id],
        tool_name="emit_extraction",
        image_b64=image_b64,
        image_media_type=media_type,
    )

    if result.error or not result.tool_input:
        return VisionPageResult(
            page_number=page_number,
            model_run_id=result.model_run_id,
            error=result.error or "no tool_input emitted",
            fact_count=0,
        )

    emitted = result.tool_input
    fact_count = 0

    # Two-pass: collect pending (anchor, fact) pairs, then flush anchors
    # so we have ids, then attach those ids to facts, then commit.
    pending: list[tuple[EvidenceAnchor, ExtractedFact]] = []

    def add(fact_type: str, **kwargs: object) -> None:
        nonlocal fact_count
        anchor = EvidenceAnchor(
            source_document_id=source.id,
            anchor_type="pdf_page",
            page_number=page_number,
            text_excerpt=(str(kwargs.get("text_excerpt") or ""))[:2000] or None,
        )
        label = str(kwargs.get("label", ""))[:512] or "(unlabeled)"
        confidence_label = str(kwargs.get("confidence_label") or "").lower()
        review_state = review_state_for_vision(label, confidence_label)

        # docs/07 Priority 1: every needs_review item should carry a
        # plain-language reason so the user knows what they're being
        # asked. Provider/contact extractions are by-default eligible
        # for source-only disposition.
        why_code: str | None = None
        why_text: str | None = None
        task_type: str | None = None
        source_only_eligible = False
        if review_state == "needs_review":
            if fact_type == "provider_relationship":
                why_code = "vision_provider_contact"
                why_text = (
                    "Extracted contact / provider name from a scanned "
                    "page. Often a fax cover sheet or signature block."
                )
                task_type = "provider_contact_cleanup"
                source_only_eligible = True
            else:
                why_code = "vision_extracted_low_confidence"
                why_text = (
                    "Extracted from a scanned image by Claude vision; "
                    f"confidence reported as {confidence_label or 'unknown'}."
                )
                task_type = "confirm_event"

        fact = ExtractedFact(
            fact_type=fact_type,
            label=label,
            description=kwargs.get("description"),
            date_start=kwargs.get("date_start"),
            date_end=kwargs.get("date_end"),
            date_precision=kwargs.get("date_precision"),
            body_site=kwargs.get("body_site"),
            laterality=kwargs.get("laterality"),
            confidence=_CONFIDENCE_INT.get(confidence_label),
            review_state=review_state,
            evidence_anchor_ids=[],
            extraction_method="claude_vision_v1",
            model_run_id=result.model_run_id,
            why_needs_review_code=why_code,
            why_needs_review_text=why_text,
            review_task_type=task_type,
            source_context_only_eligible=source_only_eligible,
        )
        pending.append((anchor, fact))
        fact_count += 1

    # Conditions
    for c in emitted.get("conditions", []) or []:
        add(
            "condition",
            label=c.get("label"),
            description=None,
            body_site=c.get("body_site"),
            laterality=c.get("laterality"),
            date_start=_date_from_emit(c.get("date_observed")),
            date_precision=c.get("date_precision"),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Procedures
    for c in emitted.get("procedures", []) or []:
        add(
            "procedure",
            label=c.get("label"),
            description=c.get("provider"),
            body_site=c.get("body_site"),
            laterality=c.get("laterality"),
            date_start=_date_from_emit(c.get("date")),
            date_precision=c.get("date_precision"),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Medications
    for c in emitted.get("medications", []) or []:
        dose = c.get("dose") or ""
        route = c.get("route") or ""
        desc = " ".join(s for s in [dose, route] if s)
        add(
            "medication",
            label=c.get("label"),
            description=desc or None,
            date_start=_date_from_emit(c.get("date_started")),
            date_end=_date_from_emit(c.get("date_stopped")),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Symptoms
    for c in emitted.get("symptoms", []) or []:
        add(
            "symptom",
            label=c.get("label"),
            date_start=_date_from_emit(c.get("date")),
            date_precision=c.get("date_precision"),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Providers (no fact — treat as life_context_event so they appear on the timeline only if dated)
    for p in emitted.get("providers", []) or []:
        add(
            "provider_relationship",
            label=p.get("name") or "(provider)",
            description=" | ".join(s for s in [p.get("role"), p.get("organization")] if s) or None,
            text_excerpt=p.get("text_excerpt"),
        )

    # Persist anchors first to get IDs, then attach to facts, then commit.
    for anchor, _claim in pending:
        db.add(anchor)
    if pending:
        await db.flush()
        for anchor, fact in pending:
            fact.evidence_anchor_ids = [anchor.id]
            db.add(fact)
        await db.commit()

    # Stitch reviewer notes into the page anchor itself for visibility.
    notes = (emitted.get("notes_to_reviewer") or "").strip()
    if notes:
        existing = page_anchor.text_excerpt or ""
        page_anchor.text_excerpt = (existing + ("\n---\n" if existing else "") + f"reviewer note: {notes}")[:2000]
        await db.commit()

    log.info(
        "vision_page_extracted",
        source_id=str(source.id),
        page=page_number,
        fact_count=fact_count,
        model_run_id=str(result.model_run_id),
    )
    return VisionPageResult(
        page_number=page_number,
        model_run_id=result.model_run_id,
        error=None,
        fact_count=fact_count,
    )


async def extract_source_pages(
    db: AsyncSession,
    user: User,
    source: SourceDocument,
    *,
    only_pages: list[int] | None = None,
    patient_context: str | None = None,
) -> VisionExtractionSummary:
    """Extract every (or a subset of) page in a previously-rendered PDF source.

    Reads page paths from `source.raw_metadata['page_renders']` populated by
    pdf.render_pdf. Looks up per-page pdf_page anchors and uses them as the
    page-level handle for each call.
    """
    page_renders = (source.raw_metadata or {}).get("page_renders", []) or []
    if not page_renders:
        return VisionExtractionSummary(source_id=source.id, pages=[])

    # Pull the per-page pdf_page anchors so vision can write reviewer notes
    # back onto them.
    from sqlalchemy import select

    res = await db.execute(
        select(EvidenceAnchor)
        .where(EvidenceAnchor.source_document_id == source.id)
        .where(EvidenceAnchor.anchor_type == "pdf_page")
    )
    by_page: dict[int, EvidenceAnchor] = {}
    for a in res.scalars().all():
        if a.page_number is not None:
            by_page[a.page_number] = a

    out: list[VisionPageResult] = []
    for entry in page_renders:
        page_n = int(entry.get("page", 0))
        if only_pages is not None and page_n not in only_pages:
            continue
        path = Path(str(entry.get("image_path", "")))
        anchor = by_page.get(page_n)
        if anchor is None:
            out.append(
                VisionPageResult(
                    page_number=page_n,
                    model_run_id=None,
                    error="page anchor missing",
                    fact_count=0,
                )
            )
            continue
        try:
            res = await extract_page(
                db, user, source,
                page_number=page_n,
                image_path=path,
                page_anchor=anchor,
                patient_context=patient_context,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("vision_page_failed", source_id=str(source.id), page=page_n, error=str(e))
            res = VisionPageResult(page_number=page_n, model_run_id=None, error=str(e), fact_count=0)
        out.append(res)
    return VisionExtractionSummary(source_id=source.id, pages=out)
