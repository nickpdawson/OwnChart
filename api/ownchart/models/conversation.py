import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class Conversation(Base, TimestampMixin):
    """A saved, searchable AI research thread (docs/10).

    Used for Ask, Make Sense follow-ups, Episode Intelligence, and
    dossier/source/period follow-ups. The scope JSON describes what
    the thread is about so retrieval can re-scope on each new message.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(512))
    # ask | make_sense | episode_intelligence | dossier_followup |
    # source_followup | review_triage
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="ask")
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    privacy_mode: Mapped[str | None] = mapped_column(String(32))
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(Base):
    """One message in a Conversation. User / assistant / system / tool."""

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(255))
    privacy_mode: Mapped[str | None] = mapped_column(String(32))
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    structured_output: Mapped[dict | None] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ConversationCitation(Base):
    """One cited evidence chip beneath an assistant message."""

    __tablename__ = "conversation_citations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # fact | source | anchor | episode | candidate | event
    citation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # source_backed | user_canonical | inferred | statistical | unknown
    claim_label: Mapped[str | None] = mapped_column(String(32))
    excerpt: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(String(512))
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
