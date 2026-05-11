import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class EvidenceAnchor(Base, TimestampMixin):
    """A pointer into a source document — page, section, line range, bbox, etc."""

    __tablename__ = "evidence_anchors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # "pdf_page", "ccda_section", "fhir_path", "csv_row", "image_bbox", "transcript_range"
    anchor_type: Mapped[str] = mapped_column(String(64), nullable=False)

    page_number: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(String(1024))
    line_range: Mapped[str | None] = mapped_column(String(64))     # e.g. "120-145"
    bbox: Mapped[dict | None] = mapped_column(JSONB)               # {x, y, w, h, page}
    timestamp_range: Mapped[str | None] = mapped_column(String(64))

    text_excerpt: Mapped[str | None] = mapped_column(String)
