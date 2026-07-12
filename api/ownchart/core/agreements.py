"""Agreement-acceptance recording (GDPR Art. 9 explicit-consent evidence).

Thin helpers shared by the register route and the authenticated
invite-accept route so both record consent identically.

Design contract:

  - Recording is OPT-IN by payload. A request that omits
    ``agreement_version`` records nothing and returns ``None`` — this is
    what keeps the default self-host register/accept flows (which don't
    send an agreement) behaving exactly as before. A tenant instance that
    wants Art. 9 evidence sends the version + flags.

  - ``consent_flags`` is stored verbatim (normalized only to guarantee a
    JSON-safe ``dict``) so the exact consent shape at acceptance time is
    preserved as evidence.

  - The pure functions (``normalize_consent_flags``,
    ``build_agreement_acceptance``) carry the logic so it can be pinned in
    unit tests without a DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agreement_acceptance import AgreementAcceptance


def normalize_consent_flags(flags: dict | None) -> dict:
    """Coerce a client-supplied consent-flags object into a JSON-safe dict.

    Non-dict input (``None``, a list, a stray string) collapses to ``{}``
    so a malformed payload never blows up the transaction — the presence
    of ``agreement_version`` is the real signal that consent was given;
    the flags are supporting detail. Values are preserved verbatim.
    """
    if not isinstance(flags, dict):
        return {}
    # Keys must be strings for JSONB; coerce defensively.
    return {str(k): v for k, v in flags.items()}


def build_agreement_acceptance(
    *,
    user_id: uuid.UUID,
    agreement_version: str,
    consent_flags: dict | None,
    context: str | None,
    now: datetime | None = None,
) -> AgreementAcceptance:
    """Construct an ``AgreementAcceptance`` row (pure — no DB)."""
    return AgreementAcceptance(
        user_id=user_id,
        agreement_version=agreement_version.strip(),
        consent_flags=normalize_consent_flags(consent_flags),
        context=context,
        accepted_at=now or datetime.now(timezone.utc),
    )


async def record_agreement_acceptance(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    agreement_version: str | None,
    consent_flags: dict | None,
    context: str | None,
    now: datetime | None = None,
) -> AgreementAcceptance | None:
    """Add an acceptance row IFF a non-empty ``agreement_version`` is given.

    Returns the row added to the session (uncommitted — the caller owns the
    transaction) or ``None`` when no version was supplied. Idempotency is
    not enforced here: append-only history means a repeated accept simply
    records another datapoint, which is the correct evidentiary behavior.
    """
    if not agreement_version or not agreement_version.strip():
        return None
    row = build_agreement_acceptance(
        user_id=user_id,
        agreement_version=agreement_version,
        consent_flags=consent_flags,
        context=context,
        now=now,
    )
    db.add(row)
    return row


async def load_latest_agreement(
    db: AsyncSession, user_id: uuid.UUID,
) -> AgreementAcceptance | None:
    """Return the user's most-recent acceptance, or ``None`` if they've
    never accepted an agreement on this instance."""
    return (await db.execute(
        select(AgreementAcceptance)
        .where(AgreementAcceptance.user_id == user_id)
        .order_by(AgreementAcceptance.accepted_at.desc())
        .limit(1)
    )).scalar_one_or_none()
