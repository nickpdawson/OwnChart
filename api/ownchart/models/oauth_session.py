import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class OAuthSession(Base, TimestampMixin):
    """Transient PKCE / OAuth state — 10 min TTL.

    The row's `id` IS the OAuth `state` parameter (UUID4 has plenty of entropy).
    On callback we look up by id, verify TTL, retrieve the pkce_verifier,
    and delete on use (single-use).
    """

    __tablename__ = "oauth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_connectors.id", ondelete="CASCADE"), nullable=False
    )

    # Raw verifier (43-128 chars per PKCE spec). Single-use.
    pkce_verifier: Mapped[str] = mapped_column(String(255), nullable=False)

    # Where to send the user after success — defaults to /connectors.
    redirect_back_to: Mapped[str | None] = mapped_column(String(512))

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
