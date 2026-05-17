import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class BriefMessage(Base):
    """One turn in the threaded follow-up conversation about a dossier.

    Anchored to the Topic (not to a single TopicBrief generation) so the
    thread continues across brief regenerations. `topic_brief_id` records
    which brief was current when this turn was authored, for audit /
    "what did the model see" reconstruction.
    """

    __tablename__ = "brief_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    topic_brief_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topic_briefs.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # M02 perimeter (Batch 6): denormalize from parent Topic so
    # thread queries can filter by record without a join. Migration
    # 0029 added the column; 0031 flips it NOT NULL.
    person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person_records.id", ondelete="CASCADE"),
    )

    # 'user' | 'assistant'
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)

    # On assistant messages: list of {fact_id, note}
    citations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    retrieved_fact_count: Mapped[int | None] = mapped_column(Integer)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    safety_response: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
