import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class Episode(Base, TimestampMixin):
    """A canonical real-world life moment (the "May 1 an eye condition
    surgery" object).

    LLM-proposed episodes land as SensemakingCandidate(candidate_type='episode')
    first. Promotion to a canonical Episode is an explicit user
    action; `promoted_from_candidate_id` records the lineage.

    `payload` carries the open-ended Episode Intelligence output:
    recovery_windows[], notable_metrics{}, interpretation_text,
    follow_up_questions[].
    """

    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(String)
    # Episode kinds (Q-F3, 2026-05-11 PM):
    #   surgery, injury, illness, pregnancy, hospital_stay, workup,
    #   recovery_window, travel, rehab, flare, medication_course,
    #   diagnostic_workup, care_transition, mental_health_episode,
    #   other.
    # Plain String(48) so adding a new kind is code-only. The
    # recovery-window profile in canonical/episode_intelligence.py
    # looks up by kind; new kinds fall through to the default
    # profile until a tuned one is added.
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="other")
    date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    primary_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_facts.id", ondelete="SET NULL")
    )
    promoted_from_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sensemaking_candidates.id", ondelete="SET NULL")
    )
    # heuristic | llm | user | imported
    created_by: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class EpisodeMember(Base):
    """A fact / source / event / candidate / conversation that
    belongs to an Episode. Composite uniqueness so the same fact
    can't be added twice."""

    __tablename__ = "episode_members"
    __table_args__ = (
        UniqueConstraint(
            "episode_id", "member_type", "subject_id",
            name="uq_episode_members_unique",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    # fact | source | event | candidate | conversation
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # primary | component | context | followup | recovery_metric
    role: Mapped[str] = mapped_column(String(48), nullable=False, default="component")
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
