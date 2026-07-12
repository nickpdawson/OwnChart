import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
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

    # Session invalidation lever (HIGH-5). Embedded as `stv` in the
    # signed session cookie and re-checked on every cookie-authenticated
    # request. Bumping it (e.g. on password change) instantly kills every
    # outstanding web session for this user without touching device
    # tokens (which are separately revocable). Starts at 0.
    session_token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Account lockout (Batch C — instance admin surface). When True the
    # account is frozen: the login endpoint refuses it AND every live
    # credential stops authenticating (cookie sessions via the
    # session_token_version bump the disable action performs, device
    # bearer tokens via the explicit check in core/device_auth.py). The
    # row and all its data survive — this is a reversible freeze, not an
    # erasure. Enforcement lives at the auth boundary, never per-route.
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    # Email verification (Email/SMTP workstream). Only enforced when
    # `auth.require_email_verification` is true (default false / self-host).
    # On the default path a freshly-registered user is stamped verified
    # immediately so login is never gated; when the flag is on and SMTP is
    # configured, registration leaves this false and sends a verification
    # link. Pre-existing users are backfilled to true by the migration so
    # enabling the flag later never bricks an established account.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
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
