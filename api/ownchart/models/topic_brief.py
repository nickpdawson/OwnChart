import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class TopicBrief(Base):
    """Persisted Executive Brief for a Topic.

    One row per generation; the dossier surfaces the latest by
    `generated_at`. Earlier rows are kept so the user can diff
    understanding over time as new evidence lands.

    `model_run_id` cites the audit row for the Anthropic call that
    produced this brief — provenance for every persisted narrative.
    """

    __tablename__ = "topic_briefs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str] = mapped_column(String(255), nullable=False)

    narrative: Mapped[str | None] = mapped_column(String)
    well_supported: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    uncertain: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    suggested_questions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    citations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    safety_response: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
