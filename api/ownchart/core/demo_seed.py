"""Demo user seed (demo.ownchart.me).

Runs at API startup when OWNCHART_DEMO_MODE=true. Idempotent: only
creates the demo user if it doesn't already exist. PHI consent is
pre-granted on the demo user so visitors can exercise the AI partner
without being routed through the consent flow.

NEVER call this from a production seed path. The credentials are
public on purpose (demo@ownchart.me / MYHEALTHdata) — they're a
read-only entrypoint into synthetic Epic FHIR sandbox data.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .logger import get_logger
from .security import hash_password
from ..models.user import User

log = get_logger("ownchart.core.demo_seed")


async def seed_demo_user_if_needed(db: AsyncSession) -> bool:
    s = get_settings()
    if not s.demo_mode:
        return False
    email = s.demo_user_email
    existing = (await db.execute(
        select(User).where(User.email == email)
    )).scalar_one_or_none()
    if existing is not None:
        return False
    user = User(
        email=email,
        password_hash=hash_password(s.demo_user_password.get_secret_value()),
        phi_consent_granted=True,
    )
    db.add(user)
    await db.commit()
    log.info("demo_user_seeded", email=email)
    return True
