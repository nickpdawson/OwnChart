"""Invitation lifecycle — FU-MULTITENANT-ONBOARDING.

Five endpoints, scoped to the Beta 1 use case "owner creates a
single-use invite, copies the URL out of band, invitee registers."

  POST   /api/invitations           Owner creates invite. Returns
                                    the raw URL ONCE.
  GET    /api/invitations           List invites the caller created
                                    OR that target records the
                                    caller owns.
  DELETE /api/invitations/{id}      Revoke an invite (creator or
                                    record owner). Idempotent.
  POST   /api/invitations/preview   UNAUTH. Takes the token in the
                                    JSON body and returns the invite
                                    shape for the accept page. LINK-01:
                                    body-not-query so the token never
                                    lands in access logs.
  GET    /api/invitations/preview   UNAUTH. DEPRECATED — same as the
                                    POST variant but takes ?token=...
                                    (leaks into access logs). Remove
                                    once iOS 1.3 uses POST.

Acceptance happens in `routes/auth.py::register` — the register
route consumes `invite_token` and runs the membership/record
creation transaction. That keeps "make user, mark invite used,
create membership" in one SQL transaction.

PM resolution (2026-05-22) lockings reflected here:
  - No standalone "Add person record" path. Every new record
    flows through an invitation with `create_new_record=true`.
  - When `create_new_record=true`, role MUST be 'owner' (the
    invitee owns the new record they're about to create). The
    schema CHECK enforces this; the route layer rejects earlier
    with a clear 422.
  - Expiry presets: 24h / 7d / 30d at creation. Default 7d.
  - No outbound email — the owner copies the URL out of band.
  - Email binding is the verification; the register route checks
    case-insensitive equality on accept.
  - No rate limit in Beta 1.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.agreements import record_agreement_acceptance
from ..core.auth_context import AuthContext, get_auth_context
from ..core.config import get_settings
from ..core.device_auth import (
    get_user_from_device_token_or_session as get_current_user,
)
from ..core.db import get_session
from ..core.email import is_smtp_configured, send_invitation_email
from ..core.logger import get_logger
from ..core.rate_limit import enforce_rate_limit
from ..core.security import (
    generate_invite_token,
    hash_invite_token,
    invite_lookup_prefix,
)
from ..models.audit_event import AuditEvent
from ..models.invitation import Invitation
from ..models.membership import Membership
from ..models.person_record import PersonRecord
from ..models.user import User
from .auth import _consume_invite

router = APIRouter()
log = get_logger("ownchart.routes.invitations")


# ---------------------------------------------------------------------------
# Request / response shapes


_TARGET_KIND = Literal["existing_record", "new_record"]
_EXPIRY_PRESETS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_INVITE_EXPIRY_NOTES: dict[str, str] = {
    "24h": "This invitation expires in 24 hours.",
    "7d": "This invitation expires in 7 days.",
    "30d": "This invitation expires in 30 days.",
}


class CreateInviteRequest(BaseModel):
    """Body of `POST /api/invitations`.

    `target_kind` switches between the two invite shapes:
      - "existing_record": `target_person_record_id` required; role
        is the caller's choice from the membership role enum.
      - "new_record": `target_person_record_id` MUST be null; role
        is locked to 'owner'. Optional `proposed_record_name`
        pre-fills the form the invitee sees.
    """
    invited_email: EmailStr
    target_kind: _TARGET_KIND
    target_person_record_id: uuid.UUID | None = None
    proposed_record_name: str | None = Field(default=None, max_length=255)
    role: Literal["viewer", "caregiver", "owner"]
    expiry_preset: Literal["24h", "7d", "30d"] = "7d"


class InviteOut(BaseModel):
    """Shape returned by list / single-item endpoints. Never carries
    the raw token — that's only ever in the create response."""
    id: str
    invited_email: str
    target_kind: _TARGET_KIND
    target_person_record_id: str | None
    target_display_name: str | None
    proposed_record_name: str | None
    role: str
    expires_at: datetime
    created_at: datetime
    created_by_user_id: str
    accepted_at: datetime | None
    accepted_by_user_id: str | None
    revoked_at: datetime | None
    status: Literal["active", "accepted", "revoked", "expired"]


class CreateInviteResponse(InviteOut):
    """Returned ONCE on `POST /api/invitations`. The `invite_url` is
    the raw token-bearing URL the owner copies out of band. After
    the response is delivered, the URL is unrecoverable — the DB
    stores only the hash.
    """
    invite_url: str


class InvitePreviewResponse(BaseModel):
    """Public-facing preview for the accept page. Says only enough
    for the invitee to understand what they're being invited to.
    Never echoes the token, never leaks the inviter's identity."""
    invited_email: str
    role: str
    target_kind: _TARGET_KIND
    target_display_name: str | None
    proposed_record_name: str | None
    expires_at: datetime


class PreviewInviteRequest(BaseModel):
    """Body of `POST /api/invitations/preview` (LINK-01).

    Carries the raw invite token in the JSON body instead of the URL
    query string, so the token can't be captured by reverse-proxy or
    web-server access logs the way a `?token=...` GET is. The response
    shape and 410 no-state-leak behavior are identical to the GET
    variant it supersedes."""
    token: str


# ---------------------------------------------------------------------------
# Pure-function helpers


def _classify_invite_state(
    invite: Invitation, *, now: datetime | None = None,
) -> Literal["active", "accepted", "revoked", "expired"]:
    n = now or datetime.now(timezone.utc)
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= n:
        return "expired"
    return "active"


def _to_invite_out(
    invite: Invitation,
    target_record: PersonRecord | None,
) -> InviteOut:
    return InviteOut(
        id=str(invite.id),
        invited_email=invite.invited_email,
        target_kind=(
            "new_record" if invite.create_new_record else "existing_record"
        ),
        target_person_record_id=(
            str(invite.target_person_record_id)
            if invite.target_person_record_id else None
        ),
        target_display_name=(
            target_record.display_name if target_record else None
        ),
        proposed_record_name=invite.proposed_record_name,
        role=invite.role,
        expires_at=invite.expires_at,
        created_at=invite.created_at,
        created_by_user_id=str(invite.created_by_user_id),
        accepted_at=invite.accepted_at,
        accepted_by_user_id=(
            str(invite.accepted_by_user_id)
            if invite.accepted_by_user_id else None
        ),
        revoked_at=invite.revoked_at,
        status=_classify_invite_state(invite),
    )


def _compute_invite_url(
    request: Request, token: str, public_base_url: str | None,
) -> str:
    """Compose the URL the invitee opens. Prefers OWNCHART_PUBLIC_BASE_URL
    (operator-configured) over the request origin so the URL is
    correct even when the owner is on localhost behind a reverse
    proxy. Falls back to scheme://host from the request when the
    config isn't set."""
    base = (public_base_url or "").rstrip("/")
    if not base:
        # request.base_url is FastAPI's resolved base — same scheme
        # the owner used to call us, so the invite URL is consistent
        # for them.
        base = str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


def classify_authed_accept(
    *,
    create_new_record: bool,
    already_active_member: bool,
) -> Literal["ok", "new_record_for_existing_user", "already_member"]:
    """Decide what an ALREADY-AUTHENTICATED user's invite accept resolves to.

    Runs AFTER the invite has passed token/expiry/email verification
    (`_consume_invite`), so it only decides the two existing-user-specific
    branches:

      - "new_record_for_existing_user": the invite's shape is "create your
        own record", but the caller already has an account. Registration —
        not accept — is the path for that shape. Route maps this to 400.

      - "already_member": the caller already holds an active membership on
        the target record. Route maps this to 409 (idempotency is NOT
        assumed — we surface the conflict so the client can just switch to
        the record it already has).

      - "ok": create the membership on the target record.
    """
    if create_new_record:
        return "new_record_for_existing_user"
    if already_active_member:
        return "already_member"
    return "ok"


async def _user_owns_record(
    db: AsyncSession, user_id: uuid.UUID, record_id: uuid.UUID,
) -> bool:
    row = (await db.execute(
        select(Membership)
        .where(Membership.user_id == user_id)
        .where(Membership.person_record_id == record_id)
        .where(Membership.role == "owner")
        .where(Membership.revoked_at.is_(None))
        .limit(1)
    )).scalar_one_or_none()
    return row is not None


# ---------------------------------------------------------------------------
# POST /api/invitations


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: CreateInviteRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> CreateInviteResponse:
    """Owner-issued invite.

    Authorization model:
      - "existing_record" invites require the caller to be `owner`
        of the target record OR `is_instance_admin`.
      - "new_record" invites require the caller to be `is_instance_admin`
        OR `owner` of at least one existing record (i.e. has standing
        in the multi-tenant fabric of this instance). The latter
        gates a non-admin owner from spawning unlimited new records
        for arbitrary people; they at least need to be an owner
        themselves before they can grant ownership to others.

    Email-uniqueness: we don't block duplicate active invites to the
    same email. PM resolution (#6, no rate limit) defers that
    decision; the listing UI surfaces every outstanding invite so
    the owner can revoke stale ones.
    """
    now = datetime.now(timezone.utc)
    invited_email = body.invited_email.lower().strip()

    if body.target_kind == "existing_record":
        if body.target_person_record_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "target_required",
                    "message": (
                        "target_person_record_id is required when "
                        "target_kind='existing_record'."
                    ),
                },
            )
        # Owner-of-target OR admin.
        if not ctx.user.is_instance_admin:
            owns = await _user_owns_record(
                db, ctx.user.id, body.target_person_record_id,
            )
            if not owns:
                # Don't leak whether the record exists — same response
                # as "you don't have access."
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "not_owner_of_record",
                        "message": (
                            "Only owners of this record can invite users to it."
                        ),
                    },
                )
        create_new_record = False
        target_record_id = body.target_person_record_id
        if body.role not in ("viewer", "caregiver", "owner"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_role", "message": "role invalid"},
            )
    else:
        # new_record path
        if body.target_person_record_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "target_xor_violation",
                    "message": (
                        "target_person_record_id must be null when "
                        "target_kind='new_record'."
                    ),
                },
            )
        if body.role != "owner":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "new_record_role_must_be_owner",
                    "message": (
                        "Invites that create a new record can only "
                        "grant 'owner'."
                    ),
                },
            )
        # Caller must be admin or own at least one record.
        if not ctx.user.is_instance_admin:
            owns_any = (await db.execute(
                select(Membership)
                .where(Membership.user_id == ctx.user.id)
                .where(Membership.role == "owner")
                .where(Membership.revoked_at.is_(None))
                .limit(1)
            )).scalar_one_or_none()
            if owns_any is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "not_an_owner",
                        "message": (
                            "Only owners or instance admins can issue "
                            "'create your own record' invites."
                        ),
                    },
                )
        create_new_record = True
        target_record_id = None

    raw_token = generate_invite_token()
    token_hash = hash_invite_token(raw_token)
    lookup_prefix = invite_lookup_prefix(raw_token)
    expires_at = now + _EXPIRY_PRESETS[body.expiry_preset]

    invite = Invitation(
        invited_email=invited_email,
        target_person_record_id=target_record_id,
        create_new_record=create_new_record,
        proposed_record_name=body.proposed_record_name,
        role=body.role,
        token_hash=token_hash,
        token_lookup_prefix=lookup_prefix,
        expires_at=expires_at,
        created_by_user_id=ctx.user.id,
        created_at=now,
    )
    db.add(invite)

    # Audit. Hash the email so the audit log doesn't become a PII
    # mining surface. Person-record scope when we know it; null
    # when create_new_record (the record doesn't exist yet).
    import hashlib
    email_hash = hashlib.sha256(invited_email.encode()).hexdigest()
    db.add(AuditEvent(
        user_id=ctx.user.id,
        person_record_id=target_record_id,
        event_type="invitation_created",
        subject_type="invitation",
        subject_id=str(invite.id),
        detail={
            "invited_email_hash": email_hash,
            "target_kind": body.target_kind,
            "role": body.role,
            "expiry_preset": body.expiry_preset,
        },
    ))
    await db.commit()
    await db.refresh(invite)

    target_record: PersonRecord | None = None
    if target_record_id is not None:
        target_record = await db.get(PersonRecord, target_record_id)

    base_url = None
    try:
        from ..core.config import get_settings
        base_url = get_settings().public_base_url or None
    except Exception:
        base_url = None
    invite_url = _compute_invite_url(request, raw_token, base_url)

    # Best-effort email delivery on top of the always-returned URL. When SMTP
    # is unconfigured this no-ops (self-hoster copies the URL by hand); when a
    # send fails we log and continue so a flaky MTA never fails invite
    # creation. The raw URL in the response is the authoritative fallback.
    if is_smtp_configured():
        try:
            await send_invitation_email(
                to=invited_email,
                invite_url=invite_url,
                role=body.role,
                expiry_note=_INVITE_EXPIRY_NOTES[body.expiry_preset],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "invitation_email_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    out = _to_invite_out(invite, target_record)
    return CreateInviteResponse(**out.model_dump(), invite_url=invite_url)


# ---------------------------------------------------------------------------
# GET /api/invitations


@router.get("")
async def list_invitations(
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> list[InviteOut]:
    """List invites visible to the caller.

    Visibility model: callers see
      - invites they created themselves, AND
      - invites targeting records they own.

    Instance admins additionally see all invites in the system.

    Ordering: most-recently-created first.
    """
    # Memberships where caller is owner.
    owned_ids_q = (
        select(Membership.person_record_id)
        .where(Membership.user_id == ctx.user.id)
        .where(Membership.role == "owner")
        .where(Membership.revoked_at.is_(None))
    )

    if ctx.user.is_instance_admin:
        q = select(Invitation).order_by(Invitation.created_at.desc())
    else:
        q = (
            select(Invitation)
            .where(
                or_(
                    Invitation.created_by_user_id == ctx.user.id,
                    Invitation.target_person_record_id.in_(owned_ids_q),
                )
            )
            .order_by(Invitation.created_at.desc())
        )

    rows = (await db.execute(q)).scalars().all()
    # Bulk-load target records for display_name in one query.
    target_ids = {
        r.target_person_record_id for r in rows
        if r.target_person_record_id is not None
    }
    targets: dict[uuid.UUID, PersonRecord] = {}
    if target_ids:
        target_rows = (await db.execute(
            select(PersonRecord).where(PersonRecord.id.in_(target_ids))
        )).scalars().all()
        targets = {t.id: t for t in target_rows}
    return [
        _to_invite_out(r, targets.get(r.target_person_record_id))
        for r in rows
    ]


# ---------------------------------------------------------------------------
# DELETE /api/invitations/{id}


@router.delete("/{invitation_id}")
async def revoke_invitation(
    invitation_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke an invite. Idempotent — revoking an already-revoked
    invite returns 200 and leaves the row alone.

    Authorization: caller must be the invite's creator OR an owner
    of the target record OR an instance admin.
    """
    invite = await db.get(Invitation, invitation_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "invitation_not_found", "message": "Not found."},
        )

    allowed = False
    if ctx.user.is_instance_admin:
        allowed = True
    elif invite.created_by_user_id == ctx.user.id:
        allowed = True
    elif invite.target_person_record_id is not None:
        if await _user_owns_record(
            db, ctx.user.id, invite.target_person_record_id,
        ):
            allowed = True
    if not allowed:
        # 404 not 403 to avoid leaking invite existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "invitation_not_found", "message": "Not found."},
        )

    if invite.accepted_at is not None:
        # Already accepted — nothing to revoke. Return 409 so the UI
        # can show "this invite has already been used."
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "invitation_already_accepted",
                "message": "This invite has already been accepted.",
            },
        )
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(invite)
    return {"ok": True, "id": str(invite.id), "status": "revoked"}


# ---------------------------------------------------------------------------
# Invite preview  (unauthenticated) — GET (deprecated) + POST (LINK-01)


async def _resolve_invite_preview(
    token: str,
    request: Request,
    db: AsyncSession,
) -> InvitePreviewResponse:
    """Shared preview resolution for the GET and POST variants.

    Looks up by lookup prefix, then verifies the token hash. Returns
    410 Gone for expired / accepted / revoked / unknown invites — the
    SAME response shape regardless, so we don't leak which terminal
    state the invite is in.

    Applies the SAME per-IP `invite_preview` rate-limit bucket for both
    entrypoints. Never echoes the token back, never logs it, never leaks
    the inviter's identity. Only the minimum the invitee needs to
    understand the invite they're about to accept.
    """
    from ..core.security import verify_invite_token

    # HIGH-7: this endpoint is unauthenticated and enumeration-shaped —
    # an attacker can probe token prefixes to find live invites. Throttle
    # per IP with a dedicated (tighter) bucket. No per-identifier
    # dimension: the token itself is the secret and must not seed a
    # counter key. This is the Lapland tenant launch gate's public face.
    _s = get_settings()
    await enforce_rate_limit(
        request,
        bucket="invite_preview",
        max_attempts=_s.rate_limit_invite_preview_max_attempts,
        window_seconds=_s.rate_limit_invite_preview_window_seconds,
    )

    if not token or len(token) < 12:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "invitation_unavailable",
                "message": "This invite is no longer available.",
            },
        )
    prefix = invite_lookup_prefix(token)
    candidates = (await db.execute(
        select(Invitation).where(Invitation.token_lookup_prefix == prefix)
    )).scalars().all()
    invite: Invitation | None = None
    for cand in candidates:
        if verify_invite_token(token, cand.token_hash):
            invite = cand
            break
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "invitation_unavailable",
                "message": "This invite is no longer available.",
            },
        )
    state = _classify_invite_state(invite)
    if state != "active":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "invitation_unavailable",
                "message": "This invite is no longer available.",
            },
        )

    target_record: PersonRecord | None = None
    if invite.target_person_record_id is not None:
        target_record = await db.get(
            PersonRecord, invite.target_person_record_id,
        )

    return InvitePreviewResponse(
        invited_email=invite.invited_email,
        role=invite.role,
        target_kind=(
            "new_record" if invite.create_new_record else "existing_record"
        ),
        target_display_name=(
            target_record.display_name if target_record else None
        ),
        proposed_record_name=invite.proposed_record_name,
        expires_at=invite.expires_at,
    )


@router.post("/preview")
async def preview_invitation_post(
    body: PreviewInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> InvitePreviewResponse:
    """Public preview for the /invite/[token] accept page (LINK-01).

    The token travels in the JSON body, NOT the URL, so it is never
    written to reverse-proxy / web-server access logs. This is the
    preferred entrypoint; the GET variant below is deprecated.
    """
    return await _resolve_invite_preview(body.token, request, db)


@router.get("/preview")
async def preview_invitation(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> InvitePreviewResponse:
    """DEPRECATED — use `POST /api/invitations/preview` (LINK-01).

    The token rides in the query string here, which leaks it into
    reverse-proxy / access logs. Kept only until the iOS 1.3 client
    switches to the POST/body variant; remove this route once iOS 1.3
    has shipped and no client calls GET .../preview anymore.
    """
    return await _resolve_invite_preview(token, request, db)


# ---------------------------------------------------------------------------
# POST /api/invitations/accept  (authenticated)


class AcceptInviteRequest(BaseModel):
    """Body of `POST /api/invitations/accept`.

    An already-authenticated user (iOS deep-link, or web while signed in)
    joins an existing record via an invite. `token` is the raw invite
    token from the deep-link. The optional agreement fields record Art. 9
    consent evidence when a tenant instance sends them (see register)."""
    token: str
    agreement_version: str | None = None
    consent_flags: dict | None = None


class AcceptInviteResponse(BaseModel):
    """The exact contract iOS built against (TENANT_IOS_RECORDS_REPORT §5)."""
    person_record_id: str
    role: str
    display_name: str


@router.post("/accept")
async def accept_invitation(
    body: AcceptInviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AcceptInviteResponse:
    """Authenticated invite acceptance — an existing user joins another
    record (iOS handoff, TENANT_IOS_RECORDS_REPORT §5).

    Registration owns the *new-user* accept path; this owns the case where
    the person is already signed in when they open the invite deep-link.

    Token verification reuses `_consume_invite` verbatim (argon2 hash +
    lookup prefix, `FOR UPDATE` single-use lock, expiry, and email-binding
    against the CALLER's email). The invite is consumed atomically — its
    `accepted_at` / `accepted_by_user_id` are stamped in the same
    transaction as the new membership.

    Guard decisions:
      - A "create your own record" invite (`create_new_record`) is rejected
        with **400** — the user already has an account; that shape belongs
        to registration, not accept.
      - If the caller already holds an active membership on the target
        record, **409 already_a_member** (non-idempotent by choice: the
        client should just switch to the record it already has, rather
        than silently succeed and imply a state change happened).
      - Consumed / expired / revoked / unknown token → **410**; email
        mismatch → **403** (both inherited from `_consume_invite`).

    We do NOT change the caller's `default_person_record_id`: they already
    have records and choose the active one explicitly via the record
    switcher / `X-OwnChart-Person-Record`. Joining a record must not
    silently repoint their default.
    """
    now = datetime.now(timezone.utc)

    # Same verification + single-use lock as registration. Binds to the
    # signed-in caller's email, so an invite issued to someone else can't
    # be accepted here (403 invite_email_mismatch from _consume_invite).
    invite = await _consume_invite(
        db, raw_token=body.token, invitee_email=user.email,
    )

    # For an existing record we must know whether the caller is already a
    # member before classifying; a new-record invite has no target to check.
    already_member = False
    if not invite.create_new_record and invite.target_person_record_id is not None:
        already_member = (await db.execute(
            select(Membership)
            .where(Membership.user_id == user.id)
            .where(Membership.person_record_id == invite.target_person_record_id)
            .where(Membership.revoked_at.is_(None))
            .limit(1)
        )).scalar_one_or_none() is not None

    verdict = classify_authed_accept(
        create_new_record=invite.create_new_record,
        already_active_member=already_member,
    )

    if verdict == "new_record_for_existing_user":
        # Do NOT consume the invite — a genuine new user can still use it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invite_creates_new_record",
                "message": (
                    "This invite creates a new account. You're already "
                    "signed in — it can't be used to add a record to your "
                    "existing account."
                ),
            },
        )
    if verdict == "already_member":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "already_a_member",
                "message": (
                    "You already have access to this record. Switch to it "
                    "instead of accepting the invite again."
                ),
            },
        )

    target_id = invite.target_person_record_id
    assert target_id is not None  # guaranteed: create_new_record is False here
    record = await db.get(PersonRecord, target_id)
    if record is None or record.disconnected_at is not None:
        # The target record was removed/disconnected out from under the
        # invite. Same terminal shape as any other unavailable invite.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": "invitation_unavailable",
                "message": "This invite is no longer available.",
            },
        )

    membership = Membership(
        user_id=user.id,
        person_record_id=target_id,
        role=invite.role,
        invited_by_user_id=invite.created_by_user_id,
        invited_at=invite.created_at,
        accepted_at=now,
        created_at=now,
    )
    db.add(membership)

    invite.accepted_at = now
    invite.accepted_by_user_id = user.id

    import hashlib
    email_hash = hashlib.sha256(invite.invited_email.encode()).hexdigest()
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=target_id,
        event_type="membership_created",
        subject_type="membership",
        subject_id=str(membership.id),
        detail={
            "role": invite.role,
            "granted_via": "invitation_accept",
            "invitation_id": str(invite.id),
        },
    ))
    db.add(AuditEvent(
        user_id=user.id,
        person_record_id=target_id,
        event_type="invitation_accepted",
        subject_type="invitation",
        subject_id=str(invite.id),
        detail={
            "invited_email_hash": email_hash,
            "role": invite.role,
            "target_kind": "existing_record",
            "accepted_while_authenticated": True,
        },
    ))

    # Art. 9 consent evidence — records a row only when a version was sent.
    await record_agreement_acceptance(
        db,
        user_id=user.id,
        agreement_version=body.agreement_version,
        consent_flags=body.consent_flags,
        context="invite_accept",
        now=now,
    )

    await db.commit()

    return AcceptInviteResponse(
        person_record_id=str(target_id),
        role=invite.role,
        display_name=record.display_name,
    )
