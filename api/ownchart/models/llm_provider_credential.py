import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class LlmProviderCredential(Base, TimestampMixin):
    """One LLM provider credential.

    `user_id IS NULL` means the deployment default (admin-managed) —
    every user inherits it unless they've added their own row for the
    same provider.

    `auth_kind`:
      - `api_key`        → `encrypted_secret` is the encrypted blob.
      - `oauth`          → `encrypted_refresh_token` +
                           `encrypted_access_token` + `oauth_expires_at`.
      - `local_endpoint` → `endpoint_url` is the base URL (no auth).

    Encrypted columns use the same envelope `core/crypto.py` provides
    for OAuth refresh tokens elsewhere. Plaintext never lands here.
    """

    __tablename__ = "llm_provider_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    # anthropic | openai | gemini | local | azure_openai
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # api_key | oauth | local_endpoint
    auth_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    endpoint_url: Mapped[str | None] = mapped_column(String(512))
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    oauth_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str | None] = mapped_column(String(128))
    default_model: Mapped[str | None] = mapped_column(String(128))
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
