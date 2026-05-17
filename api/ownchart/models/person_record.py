"""PersonRecord — the body/life/health record being analyzed.

Distinct from `User` (login identity). A user has a `User` row for
auth + identity, and N `PersonRecord` rows for the bodies they have
access to via `Membership`.

See `Working Docs/BE-1_multi_person_architecture_sketch_2026_05_17.md`
for the design rationale. Constraints + indexes match migration
0027_person_records_and_memberships.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class PersonRecord(Base):
    __tablename__ = "person_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    # "Me" / "Mom" / "Avery T. Walker" — UI affordance.
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    given_names: Mapped[str | None] = mapped_column(String(255))
    family_name: Mapped[str | None] = mapped_column(String(255))
    birth_date: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(64))

    # True when this record describes the user's own body. Informational
    # only — does NOT grant access. Access is membership-only.
    is_self: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    # Provenance — who created the record. Distinct from the owner
    # membership (which can be transferred). RESTRICT means a user
    # cannot be deleted if they're the canonical creator; the row
    # outlives the user. If real-world deletion is needed, transfer
    # creation provenance to another user first.
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    # Soft-delete. List + detail filters add `WHERE disconnected_at
    # IS NULL`. Hard delete is admin-only and rare.
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
