import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Global PHI consent — must be True before any byte ships to Anthropic.
    phi_consent_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Server-management privilege. Does NOT confer record access —
    # admins still need an explicit Membership for each PersonRecord
    # they want to read. Granted automatically to the first user on
    # a fresh DB by migration 0028. Toggled via admin UI later.
    is_instance_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    # UI affordance — "Nick" or similar. Distinct from each
    # PersonRecord.display_name (which is the body, not the login).
    display_name: Mapped[str | None] = mapped_column(String(255))

    # Fallback active record when a request carries no
    # `X-OwnChart-Person-Record` header and no session pin. AuthContext
    # resolution order: header → session → default → first membership.
    default_person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person_records.id", ondelete="SET NULL"),
    )
