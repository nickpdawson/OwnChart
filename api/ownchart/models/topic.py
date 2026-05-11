import uuid

from sqlalchemy import ARRAY, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class Topic(Base, TimestampMixin):
    """A dossier topic — an injury, a condition, sleep, medication history."""

    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    # Postgres POSIX regex patterns matched case-insensitively against
    # fact label + description. Lets a topic capture vocabulary classes
    # the alias-substring path misses (e.g. operative reports that
    # describe surgery by muscle name without saying the topic's name).
    label_patterns: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    related_concepts: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
