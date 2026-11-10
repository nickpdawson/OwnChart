"""One policy check before any data leaves for a model provider.

Every LLM request funnels through `call_with_tool`, which calls
`authorize_egress` first. The audit of 2026-11-10 found the old layout
let PHI reach a provider with consent recorded as false: Home scheduled
insight generation on a cache miss with no consent check, the background
builder reloaded the user without rechecking anything, and the shared
caller logged `consent_state` after the request had already gone out.

The checks, in order (first failure wins):

  1. account disabled
  2. global PHI consent not granted
  3. `ai.privacy_mode` is `off`
  4. the acting user holds no active membership on the record in scope

Routes translate `LlmEgressDenied` to HTTP in `main.py`; background jobs
log it and stop. Provider selection is a separate fail-closed step in
`providers/registry.py` so an explicit provider choice can never resolve
to a different recipient.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.membership import Membership
from ..models.user import User
from ..settings.registry import effective

log = get_logger("ownchart.llm.egress")


class LlmEgressDenied(Exception):
    """Refused before any request left the host.

    `code` is stable for clients: account_disabled, phi_consent_required,
    ai_off, record_access_revoked.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EgressDecision:
    privacy_mode: str


async def record_membership_active(
    db: AsyncSession, user: User, person_record_id: uuid.UUID,
) -> bool:
    row = (await db.execute(
        select(Membership.id)
        .where(Membership.user_id == user.id)
        .where(Membership.person_record_id == person_record_id)
        .where(Membership.revoked_at.is_(None))
        .limit(1)
    )).scalar_one_or_none()
    return row is not None


async def authorize_egress(
    db: AsyncSession,
    user: User | None,
    *,
    person_record_id: uuid.UUID | None,
    purpose: str,
) -> EgressDecision:
    if user is None:
        raise LlmEgressDenied("no_user", "No acting user for this AI request.")
    if getattr(user, "disabled", False):
        _refuse("account_disabled", purpose, "This account is disabled.")
    if not getattr(user, "phi_consent_granted", False):
        _refuse(
            "phi_consent_required", purpose,
            "This action would send health information to an AI provider. "
            "Enable AI in Settings to continue.",
        )
    privacy_mode = await effective(db, user, "ai.privacy_mode")
    if privacy_mode == "off":
        _refuse(
            "ai_off", purpose,
            "AI is turned off for this account (LLM privacy mode is Off). "
            "Change it in Settings to continue.",
        )
    if person_record_id is not None:
        if not await record_membership_active(db, user, person_record_id):
            _refuse(
                "record_access_revoked", purpose,
                "You no longer have access to this record, so nothing from "
                "it can be sent to an AI provider.",
            )
    return EgressDecision(privacy_mode=str(privacy_mode))


def _refuse(code: str, purpose: str, message: str) -> None:
    log.warning("llm_egress_denied", code=code, purpose=purpose)
    raise LlmEgressDenied(code, message)
