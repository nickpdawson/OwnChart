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
