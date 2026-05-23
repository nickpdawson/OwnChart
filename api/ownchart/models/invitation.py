"""Invitation — owner-issued grant that lets an invitee register
into a specific membership.

FU-MULTITENANT-ONBOARDING (Beta 1 follow-up to Section B). The
invite is the only Beta 1 path to a second account or a new
person_record on an instance whose `auth.allow_self_registration`
is false (default). It composes with that gate rather than
replacing it: the register route accepts the request when EITHER
public self-registration is open OR a valid invite token is
presented.

Two target shapes (XOR'd at the schema level):

  - `target_person_record_id` set, `create_new_record=False`:
    invitee gains a membership on an existing record at the
    requested role.

  - `target_person_record_id=None`, `create_new_record=True`:
    on accept, registration also creates a new person_record
    (with `created_by_user_id=invitee`) and an owner membership.
    Role is locked to 'owner' for this shape — see the schema
    CHECK constraint.

Tokens are stored as argon2id hashes (`token_hash`) with an 8-char
indexed `token_lookup_prefix` for fast SELECT. Verification is
constant-time via argon2; the route layer takes a row-level lock
on accept to keep the single-use property.

Lifecycle:
  - Active:  accepted_at IS NULL AND revoked_at IS NULL AND
             expires_at > now()
  - Accepted: accepted_at IS NOT NULL
  - Revoked:  revoked_at IS NOT NULL
  - Expired:  expires_at <= now() (no separate column; computed)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False)
    target_person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="CASCADE"),
        nullable=True,
    )
    create_new_record: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    proposed_record_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    token_hash: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True,
    )
    token_lookup_prefix: Mapped[str] = mapped_column(
        String(8), nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    @property
    def is_active(self) -> bool:
        from datetime import datetime as _dt, timezone as _tz
        if self.accepted_at is not None or self.revoked_at is not None:
            return False
        return self.expires_at > _dt.now(_tz.utc)
