import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditEvent(Base):
    """Append-only audit log.

    docs/09 §532: every settings/consent change creates an audit
    event. docs/08 §306: every LLM job creates an audit trail. We
    fold both into one stream so "what changed and why" is a single
    timeline query.

    No `updated_at` — audit rows are immutable.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
