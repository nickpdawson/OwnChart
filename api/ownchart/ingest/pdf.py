"""PDF ingest lane.

Three things this module does:
  - text-layer extraction via PyMuPDF (fast, exact, when the PDF has selectable text)
  - per-page PNG render under {DATA_DIR}/renders/{source_id}/page-NNNN.png
  - the all-in-one `process_pdf_source` helper that ties render + per-page
    EvidenceAnchor creation + Tesseract OCR fallback together. Both
    `routes/sources.upload_pdf` and the FHIR sync (DocumentReference PDF
    attachments) use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.logger import get_logger
from ..extract import ocr as ocr_extract
from ..models.evidence_anchor import EvidenceAnchor
from ..models.source_document import SourceDocument

log = get_logger("ownchart.ingest.pdf")


@dataclass
class PageRender:
    page_number: int           # 1-indexed
    image_path: str            # absolute filesystem path to PNG
    text_layer: str | None     # selectable text from the PDF, if any
    has_text_layer: bool


@dataclass
class PdfIngest:
    page_count: int
    pages: list[PageRender] = field(default_factory=list)
    pdf_metadata: dict[str, object] = field(default_factory=dict)


def _renders_dir(source_id: str) -> Path:
    return get_settings().data_dir / "renders" / source_id


def render_pdf(blob_bytes: bytes, source_id: str, dpi: int = 200) -> PdfIngest:
    """Render every page of a PDF to PNG and capture any embedded text layer.

    Args:
      blob_bytes: full PDF bytes
      source_id: SourceDocument.id (str) — used as the renders subdirectory
      dpi: render DPI; 200 is a good fax-readable default
    """
    out_dir = _renders_dir(source_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(stream=blob_bytes, filetype="pdf")
    try:
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pages: list[PageRender] = []
        for i, page in enumerate(doc, start=1):
            png_path = out_dir / f"page-{i:04d}.png"
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(png_path)
            text = page.get_text("text") or ""
            text = text.strip()
            pages.append(
                PageRender(
                    page_number=i,
                    image_path=str(png_path),
                    text_layer=text or None,
                    has_text_layer=bool(text),
                )
            )
        meta = {
            "page_count": len(pages),
            "title": doc.metadata.get("title") if doc.metadata else None,
            "author": doc.metadata.get("author") if doc.metadata else None,
            "creator": doc.metadata.get("creator") if doc.metadata else None,
            "producer": doc.metadata.get("producer") if doc.metadata else None,
            "any_page_has_text": any(p.has_text_layer for p in pages),
        }
        log.info(
            "pdf_rendered",
            source_id=source_id,
            page_count=len(pages),
            any_text=meta["any_page_has_text"],
        )
        return PdfIngest(page_count=len(pages), pages=pages, pdf_metadata=meta)
    finally:
        doc.close()


async def process_pdf_source(
    db: AsyncSession,
    src: SourceDocument,
    raw_bytes: bytes,
    *,
    refine_source_type: bool = False,
) -> dict[str, Any]:
    """Run the standard PDF pipeline for an existing SourceDocument.

    1. Render every page to PNG (PyMuPDF).
    2. Create one `pdf_page` EvidenceAnchor per page (or refresh existing).
    3. If no page has a text layer, run a local Tesseract OCR pass and
       backfill `EvidenceAnchor.text_excerpt` for each page.
    4. Update `src.raw_metadata` with page_renders + ocr summary.

    `refine_source_type=True` lets a caller flip 'pdf' → 'fax_pdf' when the
    rendered PDF turns out to be image-only. The FHIR-attachment caller
    starts every PDF as 'pdf' and uses this to refine.

    Returns a small dict of stats for surfacing back to the caller.
    """
    ingest = render_pdf(raw_bytes, str(src.id))

    # Flip source_type if appropriate (manual upload already does this on first save).
    if refine_source_type and not ingest.pdf_metadata.get("any_page_has_text") and src.source_type == "pdf":
        src.source_type = "fax_pdf"

    # Idempotent: drop any pre-existing `pdf_page` anchors for this source so
    # re-running the pipeline doesn't duplicate. The api never re-runs today,
    # but FHIR sync may re-fetch and we want clean state.
    existing_q = await db.execute(
        select(EvidenceAnchor).where(
            EvidenceAnchor.source_document_id == src.id,
            EvidenceAnchor.anchor_type == "pdf_page",
        )
    )
    for old in existing_q.scalars().all():
        await db.delete(old)

    page_anchors: list[EvidenceAnchor] = []
    for p in ingest.pages:
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            # M02 perimeter (Batch 2c): denormalize record scope
            # from the parent SourceDocument so retrieval can filter
            # without traversing the anchor chain at every read.
            person_record_id=src.person_record_id,
            anchor_type="pdf_page",
            page_number=p.page_number,
            text_excerpt=(p.text_layer or None) and p.text_layer[:2000],
        )
        db.add(anchor)
        page_anchors.append(anchor)
    await db.flush()

    ocr_summary: dict[str, Any] = {"ran": False}
    if not ingest.pdf_metadata.get("any_page_has_text"):
        image_paths = [p.image_path for p in ingest.pages]
        results = ocr_extract.ocr_pages(image_paths)
        anchor_by_page = {a.page_number: a for a in page_anchors}
        for r in results:
            anchor = anchor_by_page.get(r.page_number)
            if anchor and r.text:
                anchor.text_excerpt = r.text[:2000]
        confs = [r.mean_confidence for r in results if r.mean_confidence is not None]
        ocr_summary = {
            "ran": True,
            "page_count": len(results),
            "total_words": sum(r.word_count for r in results),
            "mean_confidence": (sum(confs) / len(confs)) if confs else None,
        }

    meta = dict(src.raw_metadata or {})
    meta.update(ingest.pdf_metadata)
    meta["page_renders"] = [
        {"page": p.page_number, "image_path": p.image_path} for p in ingest.pages
    ]
    if ocr_summary.get("ran"):
        meta["ocr"] = ocr_summary
    src.raw_metadata = meta

    log.info(
        "pdf_source_processed",
        source_id=str(src.id),
        page_count=ingest.page_count,
        any_text=ingest.pdf_metadata.get("any_page_has_text"),
        ocr_ran=ocr_summary.get("ran", False),
    )
    return {
        "page_count": ingest.page_count,
        "any_page_has_text": ingest.pdf_metadata.get("any_page_has_text", False),
        "ocr_ran": ocr_summary.get("ran", False),
        "ocr_total_words": ocr_summary.get("total_words"),
    }
