import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class SourceDocument(Base, TimestampMixin):
    """Immutable source artifact. NEVER mutated after creation."""

    __tablename__ = "source_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # M02 perimeter: the person record this document belongs to.
    # Added by migration 0029 (NOT NULL via 0031 after backfill).
    # Registered as part of Batch 9 backfill after the
    # cross-batch model-column gap was discovered.
    person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person_records.id", ondelete="CASCADE"),
    )

    # When this document is a child of a parent archive/package (per
    # docs/03 lane 3: Epic IHE_XDM bundle, zip of CCDAs, etc.), point
    # at the parent SourceDocument. Multi-file CCDA upload without a
    # parent archive leaves this NULL.
    parent_source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # E.g. "fax_pdf", "ccda_xml", "fhir_bundle", "auto_export_csv", "patient_note",
    #      "imaging_dmg", "dicom".
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    original_filename: Mapped[str | None] = mapped_column(String(512))
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # sha256:<hex>
    mime_type: Mapped[str | None] = mapped_column(String(128))

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_system: Mapped[str | None] = mapped_column(String(255))
    source_label: Mapped[str | None] = mapped_column(String(255))

    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)

    # Image / life-event photo extensions (per docs/03).
    # captured_at is when the depicted moment happened (often != acquired_at,
    # which is when the user uploaded). For images: read from EXIF
    # DateTimeOriginal when available; otherwise null.
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exif_metadata: Mapped[dict | None] = mapped_column(JSONB)
    user_supplied_event_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_supplied_caption: Mapped[str | None] = mapped_column(String)
