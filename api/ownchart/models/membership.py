"""Membership — `(user, person_record, role)` triple.

A user's access to a person record. Soft-deletable via `revoked_at`
so the audit trail survives a revoke.

Role enforcement is a CHECK constraint at the schema level
(`memberships_role_chk`) so the model can stay a plain string —
adding a new role later is one CHECK change, not a CREATE TYPE
migration. Constants below are the canonical Python view.

See `Working Docs/BE-1_multi_person_architecture_sketch_2026_05_17.md`
+ migration 0027 for the constraint set.

PM resolution 2026-05-17 (A-5): membership revoke is per-record,
not global session invalidation. AuthContext re-checks membership
on every request; a revoked active record returns 403
`record_access_revoked` while sessions stay valid.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


# Canonical role enum. Schema-side CHECK enforces the same set.
# Order matters for `require_role()` — earlier == lower privilege.
MEMBERSHIP_ROLES: tuple[str, ...] = ("viewer", "caregiver", "owner")

MembershipRole = Literal["viewer", "caregiver", "owner"]


# Ordinal ranking for the `require_role(min_role)` helper. Higher
# rank == more privilege. A check passes when the user's rank >=
# the requested min_rank.
_ROLE_RANK: dict[str, int] = {
    "viewer":     1,
    "caregiver":  2,
    "owner":      3,
}


def role_rank(role: str) -> int:
    """Return the privilege ordinal for a role. 0 for unknowns."""
    return _ROLE_RANK.get(role, 0)


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'owner' | 'caregiver' | 'viewer' — CHECK at schema level.
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # Invitation lifecycle. Beta 1 doesn't ship the invite-by-email
    # flow (owner adds user_id directly); these timestamps are
    # populated by the future invite UX.
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Soft delete. `revoked_at IS NULL` is the "active" filter every
    # membership query carries.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
