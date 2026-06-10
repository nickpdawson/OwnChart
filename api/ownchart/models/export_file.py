"""ExportFile — one rendered file produced by an ExportJob.

Created by migration 0040 (Slice 4 export skeleton). One row per
(export_job, file_type). A job with ``requested_format='all'``
produces two rows (one ownchart_json + one txt); a job with
``requested_format='ownchart_json'``, ``'txt'``, or
``'pictal_json'`` produces one.

``person_record_id`` is denormalized from the parent job so the
record-scoped sweeps (delete-when-record-removed, audit by record)
don't need a JOIN. Same pattern as ``evidence_anchors.person_record_id``
in the alpha schema.

``storage_uri`` is ``file://data/exports/<job_id>/<filename>`` for
the local-FS skeleton. Future slices can swap to s3:// without a
schema change. ``byte_size`` + ``sha256`` are captured at write
time so the download endpoint can set ``Content-Length`` + ``Etag``
without re-reading the file.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


FILE_TYPES: tuple[str, ...] = ("ownchart_json", "txt", "pictal_json")


class ExportFile(Base, TimestampMixin):
    __tablename__ = "export_files"
    __table_args__ = (
        UniqueConstraint(
            "export_job_id", "file_type",
            name="export_files_job_type_uq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    export_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("export_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
