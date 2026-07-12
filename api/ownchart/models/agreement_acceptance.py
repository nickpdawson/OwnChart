"""AgreementAcceptance — server-side evidence of explicit consent.

GDPR Art. 9(2)(a) requires *explicit* consent to process special-category
(health) data. When a tester accepts the tester agreement / consent form,
that acceptance must be recorded server-side so the operator can evidence
it later — a client-only checkbox leaves no auditable trail.

Design:

  - Append-only. One row per acceptance event. A user re-accepting a new
    agreement version writes a NEW row; history is never mutated. The
    "current" state is the most-recent row for the user (by ``accepted_at``).

  - ``agreement_version`` is an opaque operator-defined string
    (e.g. ``"tester-agreement-2026-07-01"``). Keeping it a free string —
    rather than an enum — is what makes this forward-compatible across
    agreement revisions without a migration each time.

  - ``consent_flags`` captures the individual checkboxes the tester ticked
    (e.g. ``{"phi_processing": true, "ai_processing": true,
    "no_medical_advice_ack": true}``). Stored verbatim as JSONB so the
    exact consent shape at acceptance time is preserved for evidence.

  - ``context`` records where the acceptance was captured
    (``"register"`` | ``"invite_accept"``) for the audit narrative.

  - CASCADE on ``user_id``: if the account is deleted (Art. 17 erasure),
    the consent evidence goes with it. That is intentional — once the data
    subject is erased there is nothing left to evidence consent *for*, and
    retaining a per-user consent row would itself be residual personal data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class AgreementAcceptance(Base):
    __tablename__ = "agreement_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agreement_version: Mapped[str] = mapped_column(String(255), nullable=False)
    # The individual consent checkboxes the tester ticked, verbatim.
    consent_flags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
    )
    # Where the acceptance was captured: "register" | "invite_accept".
    context: Mapped[str | None] = mapped_column(String(32), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
