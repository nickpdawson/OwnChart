import uuid

from sqlalchemy import ARRAY, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class Topic(Base, TimestampMixin):
    """A dossier topic — an injury, a condition, sleep, medication history.

    Per-record as of migration 0032 (PM A-1, 2026-05-17). Two
    person_records on the same instance can each have a "Knee"
    topic without collision. Global UNIQUE(slug)/UNIQUE(name) were
    dropped in favor of UNIQUE(person_record_id, slug) and
    UNIQUE(person_record_id, name).
    """

    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    # Per-record scope. Topics are no longer global vocabulary;
    # each record gets its own row with the same slug/name.
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Uniqueness is now per-record (composite UNIQUE in migration
    # 0032). Removing the per-column unique=True flags keeps the
    # ORM-emitted DDL consistent with the new constraints.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
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
