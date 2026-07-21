"""Account lifecycle — self-service deletion (App Store 5.1.1(v)).

``DELETE /api/account`` deletes the authenticated user's account and the
data they solely own. The current password is required in the body as a
second factor so a stolen-but-still-valid session can't trigger an
irreversible deletion. Verified against the argon2 hash before anything
is touched.

The cascade semantics + the co-owned-record survival rule live in
``core/account_deletion.py``. On-disk evidence-blob GC is a deliberate
follow-up (the content-addressed vault is deduped across records, so a
blob may be shared with a surviving record) — the DB purge is the
primary, verifiable requirement.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.account_deletion import delete_user_account
from ..core.config import get_settings
from ..core.db import get_session
from ..core.logger import get_logger
from ..core.security import verify_password
from ..models.user import User
from .admin import ensure_admin_self_deletion_allowed, serialize_admin_mutation
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.account")
_settings = get_settings()


class DeleteAccountRequest(BaseModel):
    """Body of ``DELETE /api/account`` — password re-confirmation."""

    current_password: str


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete the authenticated user's account + solely-owned data.

    Requires the current password (argon2-verified) as confirmation. A
    co-owned PersonRecord (another user still holds an active owner
    membership) survives; a record this user solely owns is fully
    purged along with all its sources, facts, conversations, episodes,
    exports, and connected-provider state. Returns ``204`` on success
    and clears the session cookie so the now-orphaned session doesn't
    linger in the browser.

    Wrong password → ``403`` (never reveal whether the account exists to
    an attacker riding a stale session).
    """
    if not verify_password(body.current_password, user.password_hash):
        log.info("account_delete_denied_bad_password", user_id=str(user.id))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "invalid_current_password",
                "message": "Current password is incorrect.",
            },
        )

    # P2-1: an admin can't delete themselves out of the last-enabled-admin
    # slot while other users remain (serialized so concurrent deletes can't
    # both slip through). Solo self-host is always allowed — see
    # ``ensure_admin_self_deletion_allowed`` for the App Store 5.1.1(v)
    # carve-out. Only runs for an admin, so the common non-admin delete (and
    # the fake-session route tests) never touch the DB here.
    if user.is_instance_admin:
        await serialize_admin_mutation(db)
        other_active_admins = int((await db.execute(
            select(func.count()).select_from(User)
            .where(User.is_instance_admin.is_(True))
            .where(User.disabled.is_(False))
            .where(User.id != user.id)
        )).scalar_one())
        other_users = int((await db.execute(
            select(func.count()).select_from(User).where(User.id != user.id)
        )).scalar_one())
        ensure_admin_self_deletion_allowed(
            deleter_is_active_admin=not bool(getattr(user, "disabled", False)),
            other_active_admins=other_active_admins,
            other_users=other_users,
        )

    await delete_user_account(db, user)

    # Clear THIS session's cookie. Device bearer tokens were cascaded
    # away with the user row; the cookie is the only client-held artifact
    # left to invalidate here.
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(_settings.session_cookie_name, path="/")
    return resp
