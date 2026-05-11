import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class ProviderConnection(Base, TimestampMixin):
    """A user's authenticated connection to one provider.

    Tokens are stored encrypted at rest (AES-256-GCM, key in OWNCHART_TOKEN_DEK).
    The `cached_resource_counts` column lets the UI show "Last sync: 1,234 obs,
    56 conditions" without a full re-fetch.
    """

    __tablename__ = "provider_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_connectors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    patient_fhir_id: Mapped[str | None] = mapped_column(String(255))
    patient_display_name: Mapped[str | None] = mapped_column(String(512))

    # AES-256-GCM ciphertext (nonce || ct+tag). Treat as more sensitive than
    # session_secret — these are PHI-equivalent.
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scope_granted: Mapped[str | None] = mapped_column(String(1024))

    # connected | expired | revoked | error
    status: Mapped[str] = mapped_column(String(16), default="connected", nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)

    # {'Observation': 1234, 'Condition': 56, ...}
    cached_resource_counts: Mapped[dict | None] = mapped_column(JSONB)
