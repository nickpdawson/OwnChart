import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class SensemakingJob(Base, TimestampMixin):
    """One Make Sense run (manual, import-time, or nightly).

    Pairs 1:1 with a `ModelRun` when an external LLM is used; the
    ModelRun carries token usage and the rendered prompt, this row
    carries the user-facing scope (which source / topic / window) plus
    the privacy mode it ran under.
    """

    __tablename__ = "sensemaking_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # source_summary | episode_candidates | review_queue_triage |
    # label_translation | period_summary
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    # pending | running | completed | failed | refused
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # off | metadata_only | selected_evidence | full_source
    privacy_mode: Mapped[str] = mapped_column(String(32), nullable=False)

    # Scope describes what the job was asked to look at, in a shape the
    # API + UI can both render: {"source_id": "..."} or
    # {"topic_slug": "..."} or {"from": "...", "to": "..."}.
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(2000))
