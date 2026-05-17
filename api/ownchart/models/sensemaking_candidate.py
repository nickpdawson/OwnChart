import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class SensemakingCandidate(Base, TimestampMixin):
    """A proposed structure or summary from a SensemakingJob.

    Per docs/08, the LLM may propose meaning but may not silently
    decide truth. Candidates live here until the user accepts, edits,
    or dismisses them. Promoting a candidate to a canonical structure
    (Episode, Topic, source-only fact, etc.) is a separate explicit
    step; this row is never the canonical artifact.
    """

    __tablename__ = "sensemaking_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # M02 perimeter (Batch 5): denormalize record scope from parent
    # job so list / inbox queries can filter without a join.
    person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person_records.id", ondelete="CASCADE"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sensemaking_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # source_summary | episode | duplicate_group | review_task |
    # label_translation | topic | question
    candidate_type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    summary_text: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # docs/08 §340 claim labels.
    claim_label: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[int | None] = mapped_column(SmallInteger)

    source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )
    fact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )
    evidence_anchor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )

    # pending | accepted | edited | dismissed | rejected
    disposition: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    disposition_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_edit: Mapped[str | None] = mapped_column(String)
