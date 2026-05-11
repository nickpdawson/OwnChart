import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class ExtractionJob(Base):
    """Background-job record for Claude vision extraction over a PDF.

    Spans the whole multi-page run; the per-page work happens in the Arq
    worker. UI polls `GET /api/sources/{id}/extraction-status` for the
    latest job's progress.
    """

    __tablename__ = "extraction_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # pending | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)

    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    facts_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-page errors: [{"page": 4, "error": "..."}]
    page_errors: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    only_pages: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    patient_context: Mapped[str | None] = mapped_column(String)

    arq_job_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
