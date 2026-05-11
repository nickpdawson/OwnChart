import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class HealthEvent(Base, TimestampMixin):
    """The timeline-facing normalized event.

    Composes ExtractedFact(s) + (optionally) a UserAssertion. Carries
    `confidence` and `review_state` derived from its inputs.
    """

    __tablename__ = "health_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)

    topic_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )

    # condition | procedure | medication | symptom | observation |
    # encounter | imaging | document_event | life_context | inferred
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    display_title: Mapped[str] = mapped_column(String(512), nullable=False)
    display_summary: Mapped[str | None] = mapped_column(String)

    date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str | None] = mapped_column(String(16))

    source_fact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )
    canonical_assertion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_assertions.id", ondelete="SET NULL")
    )

    confidence: Mapped[int | None] = mapped_column(Integer)
    review_state: Mapped[str] = mapped_column(
        String(16), default="needs_review", nullable=False, index=True
    )

    # clinical | document | patient_reported | inferred | quantified_self | calendar
    visibility_layer: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
