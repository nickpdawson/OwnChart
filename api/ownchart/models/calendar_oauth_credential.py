"""CalendarOAuthCredential — OAuth credentials for cloud calendar adapters.

Created by migration 0042 (FU-CAL-GOOGLE-OAUTH). One row per
(user, person_record, provider, account_email). The CalendarSource
rows for Google calendars point back here via
``oauth_credential_id`` so one Google account binding can support
multiple bound Google calendars without re-OAuth per calendar.

  - ``refresh_token_enc`` is the long-lived refresh token, encrypted
    with the AES-256-GCM DEK from ``core.crypto``. NEVER read this
    column outside the worker / route layers that need a fresh
    access token; never log it; never include it in any read API.

  - ``access_token_enc`` is the most recent short-lived access
    token. Stored encrypted too so a DB dump alone can't impersonate
    a connected Google account. NULL when no token has been issued
    yet (between OAuth grant and first sync).

  - ``scope_granted`` is the space-separated scope list Google
    actually returned at consent. The route layer rejects any
    callback whose granted scope set includes anything beyond the
    read-only allowlist; this column is the audit record of what
    was granted, NOT the policy oracle. Policy lives in
    ``ingest/google_calendar.py::READ_ONLY_SCOPES``.

  - ``status`` mirrors ``provider_connections.status``:
    ``connected`` | ``expired`` | ``revoked`` | ``error``. Expired
    just means access_token_expires_at < now; the refresh flow
    recovers it. Revoked means Google returned 401 on a refresh
    attempt and the row is dead until re-consent. Error is a
    transport-class problem captured in ``last_error``.

Slice 1 perimeter — ``person_record_id`` NOT NULL at creation; no
backfill chain. Row is created inside the OAuth callback, scoped to
the signed person_record from the state param (NOT the active
record at callback time).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


GOOGLE_CALENDAR_PROVIDER = "google"

# Mirror of provider_connections.status state machine.
OAUTH_STATUSES: tuple[str, ...] = (
    "connected",
    "expired",
    "revoked",
    "error",
)


class CalendarOAuthCredential(Base, TimestampMixin):
    __tablename__ = "calendar_oauth_credentials"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "person_record_id",
            "provider",
            "google_account_email",
            name="calendar_oauth_credentials_uq",
        ),
    )

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
    provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GOOGLE_CALENDAR_PROVIDER,
    )
    google_account_email: Mapped[str] = mapped_column(
        String(320), nullable=False,
    )
    refresh_token_enc: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False,
    )
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    scope_granted: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="connected",
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    last_error: Mapped[str | None] = mapped_column(String(2048))
