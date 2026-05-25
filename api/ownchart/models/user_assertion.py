import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class UserAssertion(Base, TimestampMixin):
    """A user-confirmed or user-authored canonical layer.

    Becomes canonical for display and reasoning. Does NOT overwrite
    SourceDocument or ExtractedFact — those remain unchanged.
    """

    __tablename__ = "user_assertions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # M02 perimeter: the record this assertion applies to. The
    # underlying table got person_record_id NOT NULL via the Slice 1
    # migration set, but the SQLAlchemy model wasn't updated to map
    # it — the two PATCH /api/facts/* routes inserted UserAssertion
    # rows without the field and hit asyncpg.NotNullViolationError
    # at first user click (2026-05-25). Fixed by adding the mapped
    # column here and stamping ctx.active_record_id at every insert
    # site. Same fix shape as the earlier FU-EXTRACT-PERIMETER-MISS
    # round (#160/#162).
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    related_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_facts.id", ondelete="SET NULL"), index=True
    )

    # confirm | correct | reject | annotate | author
    assertion_type: Mapped[str] = mapped_column(String(16), nullable=False)

    canonical_label: Mapped[str | None] = mapped_column(String(512))
    canonical_description: Mapped[str | None] = mapped_column(String)
    canonical_date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canonical_date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reason: Mapped[str | None] = mapped_column(String)
