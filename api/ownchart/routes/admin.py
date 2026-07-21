"""Instance-admin surface (Batch C — the last multi-tenant item).

A hosted OwnChart operator (Nick running the Lapland tenant at
``app.ownchart.me``) needs to see and manage the testers on their
instance. Today the only "admin" primitives are the
``users.is_instance_admin`` bool + the invitations API; this module is
the surface that ties them together: list/inspect users, freeze/restore
accounts, grant/revoke admin, oversee invitations, and see instance-wide
LLM spend.

Every route is gated by ``require_instance_admin`` — the authed user
(cookie OR device bearer, via ``get_user_from_device_token_or_session``)
must carry ``is_instance_admin``. This is deliberately NOT record-scoped
(``get_auth_context``): an operator manages the instance, not a specific
person-record, and may hold admin without an active record pinned.

Guardrails that keep an operator from locking themselves out:
  - An admin cannot disable their own account.
  - Admin cannot be revoked from the last remaining instance admin
    (there is always ≥ 1 admin on the instance).

Disable enforcement lives at the auth boundary, not here: setting
``users.disabled`` + bumping ``session_token_version`` freezes cookie
sessions (stv mismatch) AND device bearer tokens (the ``disabled`` check
in ``core/device_auth.py``). See that module and ``routes/auth.py`` login.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.device_auth import (
    get_user_from_device_token_or_session as get_current_user,
)
from ..core.logger import get_logger
from ..llm.budget import (
    month_start_utc,
    month_to_date_spend_by_user,
    next_month_start_utc,
)
from ..llm.pricing import estimate_cost_usd, price_for
from ..models.agreement_acceptance import AgreementAcceptance
from ..models.audit_event import AuditEvent
from ..models.device_token import DeviceToken
from ..models.invitation import Invitation
from ..models.membership import Membership
from ..models.model_run import ModelRun
from ..models.person_record import PersonRecord
from ..models.user import User

router = APIRouter()
log = get_logger("ownchart.routes.admin")


# ---------------------------------------------------------------------------
# Gate


async def require_instance_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Dependency: the authed user must be an instance admin, else 403.

    Reuses ``get_user_from_device_token_or_session`` so both the web
    cookie and the iOS device bearer authenticate here. A disabled admin
    never reaches this — the auth dependency 401s them first.
    """
    if not user.is_instance_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "instance_admin_required",
                "message": "This is an instance-administrator surface.",
            },
        )
    return user


# ---------------------------------------------------------------------------
# Serialization of admin-count-reducing mutations (P2-1 / P1-11)
#
# Concurrent revoke-admin / disable-admin / admin-self-deletion each read the
# admin count, decide it's safe, then write. Without serialization two of them
# can both pass the last-admin check and strand the instance with zero
# administrators (or zero *active* administrators). A transaction-level
# advisory lock forces them to run one at a time so the second sees the first's
# committed effect. Distinct key from bootstrap's 0x0C0C0C01. Mirrors
# ``routes/auth.py::_serialize_bootstrap`` — no-op off Postgres (test fakes /
# SQLite), where there is no real concurrency to guard.

_ADMIN_MUTATION_LOCK_KEY = 0x0C0C_0C02


async def serialize_admin_mutation(db: AsyncSession) -> None:
    """Hold the admin-mutation advisory lock for this transaction."""
    try:
        dialect_name = db.get_bind().dialect.name
    except Exception:  # noqa: BLE001 — test fakes have no bind; nothing to lock
        return
    if dialect_name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _ADMIN_MUTATION_LOCK_KEY},
    )


# ---------------------------------------------------------------------------
# Pure guards (unit-tested without a DB)


def ensure_not_self(actor_id: uuid.UUID, target_id: uuid.UUID, *, action: str) -> None:
    """Refuse a destructive self-action (400). An admin disabling
    themselves would lock the operator out of their own instance."""
    if actor_id == target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "cannot_target_self",
                "message": f"You can't {action} your own account.",
            },
        )


def ensure_admin_remains(*, target_is_admin: bool, total_admin_count: int) -> None:
    """Refuse revoking admin from the last remaining instance admin (409).

    ``total_admin_count`` is the current number of instance admins
    (including the target). If the target is the only admin, revoking
    would leave the instance with zero admins — blocked. A no-op revoke
    (target isn't an admin) never reaches this guard.
    """
    if target_is_admin and total_admin_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_admin",
                "message": (
                    "This is the only instance administrator. Grant admin "
                    "to another account before revoking this one."
                ),
            },
        )


def ensure_active_admin_remains(
    *, target_is_active_admin: bool, active_admin_count: int,
) -> None:
    """Refuse disabling the last *enabled* admin (409).

    A disabled admin still carries ``is_instance_admin`` but can no longer
    log in, so the load-bearing count for "can anyone still administer the
    instance" is the ENABLED admin count. ``active_admin_count`` includes the
    target; if the target is the only enabled admin, disabling it would leave
    the instance with no one able to administer it.
    """
    if target_is_active_admin and active_admin_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_active_admin",
                "message": (
                    "This is the only enabled instance administrator. Enable "
                    "or grant admin to another account before disabling this "
                    "one."
                ),
            },
        )


async def _active_admin_count(db: AsyncSession) -> int:
    """Enabled (non-disabled) instance admins."""
    return int((await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_instance_admin.is_(True))
        .where(User.disabled.is_(False))
    )).scalar_one())


def ensure_admin_self_deletion_allowed(
    *, deleter_is_active_admin: bool, other_active_admins: int, other_users: int,
) -> None:
    """Refuse an admin deleting their own account when it would leave the
    instance with zero enabled admins WHILE other users remain (409).

    Solo carve-out: with no other users, self-deletion is ALWAYS allowed —
    App Store 5.1.1(v) requires a reachable in-app deletion path, and a
    single-user self-host must be able to erase itself. Only a multi-user
    instance where this is the last enabled admin is blocked, with a
    "transfer admin first" message.
    """
    if deleter_is_active_admin and other_active_admins == 0 and other_users > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_admin_transfer_required",
                "message": (
                    "You are the only enabled administrator and other users "
                    "remain on this instance. Grant admin to another account "
                    "before deleting yours."
                ),
            },
        )


def _row_cost(model: str | None, stored: Any, usage: dict[str, Any] | None) -> float:
    """Per-run USD cost for admin usage rollups. Mirrors the llm_providers
    usage view: prefer the stored column, fall back to recomputing from
    tokens for legacy rows, 0 for unpriced models."""
    if price_for(model) is None:
        return 0.0
    if stored is not None:
        return round(float(stored), 6)
    return estimate_cost_usd(model, usage if isinstance(usage, dict) else None)


# ---------------------------------------------------------------------------
# Response shapes


class AdminUserRow(BaseModel):
    id: str
    email: str
    email_verified: bool
    is_instance_admin: bool
    disabled: bool
    created_at: datetime
    membership_count: int          # active (non-revoked) memberships
    latest_agreement_version: str | None
    latest_agreement_accepted_at: datetime | None
    month_to_date_spend_usd: float


class AdminUserList(BaseModel):
    users: list[AdminUserRow]
    total: int                     # total users (ignores limit/offset)
    limit: int
    offset: int


class AdminMembershipOut(BaseModel):
    person_record_id: str
    display_name: str
    role: str
    revoked: bool
    revoked_at: datetime | None


class UsageBreakdownItem(BaseModel):
    key: str
    runs: int
    estimated_usd_cost: float


class AdminUserDetail(AdminUserRow):
    memberships: list[AdminMembershipOut]
    device_token_count: int        # active (non-revoked) device tokens
    per_model: list[UsageBreakdownItem]
    per_purpose: list[UsageBreakdownItem]


class AdminActionResult(BaseModel):
    ok: bool
    id: str
    disabled: bool | None = None
    is_instance_admin: bool | None = None


class AdminInviteOut(BaseModel):
    id: str
    invited_email: str
    target_kind: str
    target_person_record_id: str | None
    role: str
    status: str
    expires_at: datetime
    created_at: datetime
    created_by_user_id: str
    accepted_at: datetime | None
    accepted_by_user_id: str | None
    revoked_at: datetime | None


class AdminUsagePerUser(BaseModel):
    user_id: str
    email: str
    month_to_date_spend_usd: float


class AdminUsageResponse(BaseModel):
    month_start: datetime
    resets_at: datetime
    total_estimated_usd_cost: float
    per_user: list[AdminUsagePerUser]


class AdminOverview(BaseModel):
    user_count: int
    admin_count: int
    disabled_user_count: int
    record_count: int              # non-disconnected person records
    active_membership_count: int
    pending_invitation_count: int
    month_to_date_spend_usd: float


# ---------------------------------------------------------------------------
# Pure shape composers (unit-tested without a DB)


def build_user_row(
    user: User,
    *,
    membership_count: int,
    latest_agreement: AgreementAcceptance | None,
    month_spend: float,
) -> AdminUserRow:
    return AdminUserRow(
        id=str(user.id),
        email=user.email,
        email_verified=bool(user.email_verified),
        is_instance_admin=bool(user.is_instance_admin),
        disabled=bool(getattr(user, "disabled", False)),
        created_at=user.created_at,
        membership_count=membership_count,
        latest_agreement_version=(
            latest_agreement.agreement_version if latest_agreement else None
        ),
        latest_agreement_accepted_at=(
            latest_agreement.accepted_at if latest_agreement else None
        ),
        month_to_date_spend_usd=round(month_spend, 6),
    )


def _invite_status(invite: Invitation, *, now: datetime) -> str:
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    return "active"


def build_invite_out(invite: Invitation, *, now: datetime | None = None) -> AdminInviteOut:
    n = now or datetime.now(timezone.utc)
    return AdminInviteOut(
        id=str(invite.id),
        invited_email=invite.invited_email,
        target_kind=("new_record" if invite.create_new_record else "existing_record"),
        target_person_record_id=(
            str(invite.target_person_record_id)
            if invite.target_person_record_id else None
        ),
        role=invite.role,
        status=_invite_status(invite, now=n),
        expires_at=invite.expires_at,
        created_at=invite.created_at,
        created_by_user_id=str(invite.created_by_user_id),
        accepted_at=invite.accepted_at,
        accepted_by_user_id=(
            str(invite.accepted_by_user_id)
            if invite.accepted_by_user_id else None
        ),
        revoked_at=invite.revoked_at,
    )


def _breakdown_items(d: dict[str, list[float]]) -> list[UsageBreakdownItem]:
    return [
        UsageBreakdownItem(key=k, runs=int(v[0]), estimated_usd_cost=round(v[1], 6))
        for k, v in sorted(d.items(), key=lambda kv: -kv[1][1])
    ]


def summarize_runs(runs: list[ModelRun]) -> tuple[
    float, list[UsageBreakdownItem], list[UsageBreakdownItem]
]:
    """Total spend + per-model/per-purpose breakdown for a set of runs.
    Pure — takes already-fetched ModelRun rows."""
    total = 0.0
    per_model: dict[str, list[float]] = {}
    per_purpose: dict[str, list[float]] = {}
    for r in runs:
        cost = _row_cost(
            r.model, r.estimated_cost_usd,
            r.usage if isinstance(r.usage, dict) else None,
        )
        total += cost
        pm = per_model.setdefault(r.model, [0.0, 0.0])
        pm[0] += 1
        pm[1] += cost
        pp = per_purpose.setdefault(r.purpose, [0.0, 0.0])
        pp[0] += 1
        pp[1] += cost
    return round(total, 6), _breakdown_items(per_model), _breakdown_items(per_purpose)


# ---------------------------------------------------------------------------
# Batched per-user lookups (stable execute order for testability)


async def _active_membership_count_by_user(
    db: AsyncSession, user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(Membership.user_id, func.count())
        .where(Membership.user_id.in_(user_ids))
        .where(Membership.revoked_at.is_(None))
        .group_by(Membership.user_id)
    )).all()
    return {uid: int(n) for uid, n in rows}


async def _latest_agreement_by_user(
    db: AsyncSession, user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, AgreementAcceptance]:
    """Most-recent acceptance per user. Ordered newest-first; the first
    row seen per user wins (works against a plain execute + a fake)."""
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(AgreementAcceptance)
        .where(AgreementAcceptance.user_id.in_(user_ids))
        .order_by(AgreementAcceptance.accepted_at.desc())
    )).scalars().all()
    out: dict[uuid.UUID, AgreementAcceptance] = {}
    for a in rows:
        out.setdefault(a.user_id, a)
    return out


# ---------------------------------------------------------------------------
# GET /api/admin/users


@router.get("/users", response_model=AdminUserList)
async def list_users(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="created_at", pattern="^(created_at|email|spend)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    _admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminUserList:
    """Every user on the instance: identity, verification, admin/disabled
    flags, active-membership count, latest agreement acceptance, and
    current-month LLM spend.

    Sort by ``created_at`` (default), ``email``, or ``spend`` (current
    month). ``spend`` sorts in Python after the spend map is resolved
    (it isn't a column); the others sort in SQL. Paginated via
    limit/offset with a ``total`` count independent of the page.
    """
    total = int((await db.execute(
        select(func.count()).select_from(User)
    )).scalar_one())

    stmt = select(User)
    col = User.email if sort == "email" else User.created_at
    stmt = stmt.order_by(col.asc() if order == "asc" else col.desc())
    # For spend-sort we must resolve every user's spend first, so page
    # after sorting in Python; otherwise page in SQL for efficiency.
    if sort != "spend":
        stmt = stmt.offset(offset).limit(limit)
    users = list((await db.execute(stmt)).scalars().all())

    spend_by_user = await month_to_date_spend_by_user(db)

    if sort == "spend":
        users.sort(
            key=lambda u: spend_by_user.get(u.id, 0.0),
            reverse=(order == "desc"),
        )
        users = users[offset:offset + limit]

    user_ids = [u.id for u in users]
    counts = await _active_membership_count_by_user(db, user_ids)
    agreements = await _latest_agreement_by_user(db, user_ids)

    rows = [
        build_user_row(
            u,
            membership_count=counts.get(u.id, 0),
            latest_agreement=agreements.get(u.id),
            month_spend=spend_by_user.get(u.id, 0.0),
        )
        for u in users
    ]
    return AdminUserList(users=rows, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# GET /api/admin/users/{id}


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(
    user_id: uuid.UUID,
    _admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminUserDetail:
    """One user in full: the list-row fields plus every membership
    (record id/name/role/revoked), active device-token count, and a
    current-month usage breakdown by model + purpose."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    # Memberships joined to their records (include revoked — the admin
    # wants the full history, flagged).
    mem_rows = (await db.execute(
        select(Membership, PersonRecord)
        .join(PersonRecord, PersonRecord.id == Membership.person_record_id)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at.asc())
    )).all()
    memberships = [
        AdminMembershipOut(
            person_record_id=str(rec.id),
            display_name=rec.display_name,
            role=mem.role,
            revoked=mem.revoked_at is not None,
            revoked_at=mem.revoked_at,
        )
        for mem, rec in mem_rows
    ]
    active_membership_count = sum(1 for m in memberships if not m.revoked)

    device_token_count = int((await db.execute(
        select(func.count())
        .select_from(DeviceToken)
        .where(DeviceToken.user_id == user_id)
        .where(DeviceToken.revoked_at.is_(None))
    )).scalar_one())

    now = datetime.now(timezone.utc)
    start = month_start_utc(now)
    runs = list((await db.execute(
        select(ModelRun)
        .where(ModelRun.user_id == user_id)
        .where(ModelRun.created_at >= start)
    )).scalars().all())
    total_spend, per_model, per_purpose = summarize_runs(runs)

    latest_agreement = (await db.execute(
        select(AgreementAcceptance)
        .where(AgreementAcceptance.user_id == user_id)
        .order_by(AgreementAcceptance.accepted_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    base = build_user_row(
        user,
        membership_count=active_membership_count,
        latest_agreement=latest_agreement,
        month_spend=total_spend,
    )
    return AdminUserDetail(
        **base.model_dump(),
        memberships=memberships,
        device_token_count=device_token_count,
        per_model=per_model,
        per_purpose=per_purpose,
    )


# ---------------------------------------------------------------------------
# POST /api/admin/users/{id}/disable | /enable


async def _set_disabled(
    db: AsyncSession, admin: User, user_id: uuid.UUID, *, disabled: bool,
) -> AdminActionResult:
    if disabled:
        # An operator cannot lock themselves out of their own instance.
        ensure_not_self(admin.id, user_id, action="disable")

    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )

    if bool(getattr(target, "disabled", False)) == disabled:
        # Idempotent — already in the requested state. No stv churn.
        return AdminActionResult(ok=True, id=str(target.id), disabled=disabled)

    if disabled and target.is_instance_admin:
        # P2-1: disabling an admin reduces the ENABLED-admin count. Serialize
        # + refuse if this would disable the last enabled admin (a disabled
        # admin can't log in, so it would lock out instance administration).
        await serialize_admin_mutation(db)
        ensure_active_admin_remains(
            target_is_active_admin=True,
            active_admin_count=await _active_admin_count(db),
        )

    target.disabled = disabled
    if disabled:
        # Kill live cookie sessions immediately (device tokens die via the
        # `disabled` check in core/device_auth.py). Enabling does NOT
        # un-bump — the tokens the user re-issues work; old ones stay dead.
        target.session_token_version = (target.session_token_version or 0) + 1
    db.add(AuditEvent(
        user_id=admin.id,
        person_record_id=None,
        event_type=("user_disabled" if disabled else "user_enabled"),
        subject_type="user",
        subject_id=str(target.id),
        detail={
            "target_user_id": str(target.id),
            "session_token_version": target.session_token_version,
        },
    ))
    await db.commit()
    return AdminActionResult(ok=True, id=str(target.id), disabled=disabled)


@router.post("/users/{user_id}/disable", response_model=AdminActionResult)
async def disable_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminActionResult:
    """Freeze an account: refuses login, kills the user's live cookie
    session (stv bump) AND device bearer tokens (the ``disabled`` auth
    check). Reversible via /enable. An admin cannot disable themselves."""
    return await _set_disabled(db, admin, user_id, disabled=True)


@router.post("/users/{user_id}/enable", response_model=AdminActionResult)
async def enable_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminActionResult:
    """Clear the disabled flag. The user can sign in again and re-pair
    devices; previously-issued (now-dead) tokens stay revoked."""
    return await _set_disabled(db, admin, user_id, disabled=False)


# ---------------------------------------------------------------------------
# POST /api/admin/users/{id}/make-admin | /revoke-admin


async def _admin_count(db: AsyncSession) -> int:
    return int((await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_instance_admin.is_(True))
    )).scalar_one())


@router.post("/users/{user_id}/make-admin", response_model=AdminActionResult)
async def make_admin(
    user_id: uuid.UUID,
    admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminActionResult:
    """Grant instance-admin. Idempotent."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )
    if target.is_instance_admin:
        return AdminActionResult(ok=True, id=str(target.id), is_instance_admin=True)
    target.is_instance_admin = True
    db.add(AuditEvent(
        user_id=admin.id,
        person_record_id=None,
        event_type="admin_granted",
        subject_type="user",
        subject_id=str(target.id),
        detail={"target_user_id": str(target.id)},
    ))
    await db.commit()
    return AdminActionResult(ok=True, id=str(target.id), is_instance_admin=True)


@router.post("/users/{user_id}/revoke-admin", response_model=AdminActionResult)
async def revoke_admin(
    user_id: uuid.UUID,
    admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminActionResult:
    """Revoke instance-admin. Blocked if the target is the LAST admin —
    the instance must always retain at least one administrator (this also
    stops an admin from revoking their own last-admin status)."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found."},
        )
    if not target.is_instance_admin:
        # Idempotent — nothing to revoke.
        return AdminActionResult(ok=True, id=str(target.id), is_instance_admin=False)

    # P2-1: serialize so two concurrent revokes can't both pass the
    # last-admin check and strand the instance with zero admins.
    await serialize_admin_mutation(db)
    total_admins = await _admin_count(db)
    ensure_admin_remains(target_is_admin=True, total_admin_count=total_admins)

    target.is_instance_admin = False
    db.add(AuditEvent(
        user_id=admin.id,
        person_record_id=None,
        event_type="admin_revoked",
        subject_type="user",
        subject_id=str(target.id),
        detail={"target_user_id": str(target.id)},
    ))
    await db.commit()
    return AdminActionResult(ok=True, id=str(target.id), is_instance_admin=False)


# ---------------------------------------------------------------------------
# Invitations oversight


@router.get("/invitations", response_model=list[AdminInviteOut])
async def list_invitations(
    include_terminal: bool = Query(
        default=True,
        description="Include accepted/revoked/expired invites (default true).",
    ),
    _admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> list[AdminInviteOut]:
    """Every invitation on the instance, newest first. With
    ``include_terminal=false`` only outstanding (active) invites are
    returned — the ones an operator might still want to revoke."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Invitation).order_by(Invitation.created_at.desc())
    )).scalars().all()
    out = [build_invite_out(inv, now=now) for inv in rows]
    if not include_terminal:
        out = [o for o in out if o.status == "active"]
    return out


@router.post("/invitations/{invitation_id}/revoke", response_model=AdminInviteOut)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminInviteOut:
    """Revoke any invitation on the instance. Idempotent for an
    already-revoked invite; 409 if it was already accepted (nothing to
    revoke — the membership already exists)."""
    invite = await db.get(Invitation, invitation_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "invitation_not_found", "message": "Not found."},
        )
    if invite.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "invitation_already_accepted",
                "message": "This invite has already been accepted.",
            },
        )
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(timezone.utc)
        db.add(AuditEvent(
            user_id=admin.id,
            person_record_id=invite.target_person_record_id,
            event_type="invitation_revoked",
            subject_type="invitation",
            subject_id=str(invite.id),
            detail={"revoked_via": "admin"},
        ))
        await db.commit()
        await db.refresh(invite)
    return build_invite_out(invite)


# ---------------------------------------------------------------------------
# Cross-user usage (the Batch B deferral)


@router.get("/usage", response_model=AdminUsageResponse)
async def instance_usage(
    _admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminUsageResponse:
    """Instance-wide LLM spend for the current UTC month: a per-user
    breakdown (email + month-to-date spend) plus the instance total.

    Batch B scoped ``/api/llm-providers/usage`` to the calling user;
    this is the cross-user admin view it deferred. Users with zero
    spend this month are included (0.0) so the operator sees everyone.
    """
    now = datetime.now(timezone.utc)
    spend_by_user = await month_to_date_spend_by_user(db, now=now)
    users = list((await db.execute(
        select(User).order_by(User.email.asc())
    )).scalars().all())

    per_user = [
        AdminUsagePerUser(
            user_id=str(u.id),
            email=u.email,
            month_to_date_spend_usd=round(spend_by_user.get(u.id, 0.0), 6),
        )
        for u in users
    ]
    per_user.sort(key=lambda x: -x.month_to_date_spend_usd)
    total = round(sum(spend_by_user.values()), 6)
    return AdminUsageResponse(
        month_start=month_start_utc(now),
        resets_at=next_month_start_utc(now),
        total_estimated_usd_cost=total,
        per_user=per_user,
    )


# ---------------------------------------------------------------------------
# Instance overview


@router.get("/overview", response_model=AdminOverview)
async def overview(
    _admin: User = Depends(require_instance_admin),
    db: AsyncSession = Depends(get_session),
) -> AdminOverview:
    """Headline counts for the instance dashboard: users (+ admins +
    disabled), person records, active memberships, pending invites, and
    month-to-date instance spend."""
    now = datetime.now(timezone.utc)

    user_count = int((await db.execute(
        select(func.count()).select_from(User)
    )).scalar_one())
    admin_count = await _admin_count(db)
    disabled_count = int((await db.execute(
        select(func.count()).select_from(User).where(User.disabled.is_(True))
    )).scalar_one())
    record_count = int((await db.execute(
        select(func.count())
        .select_from(PersonRecord)
        .where(PersonRecord.disconnected_at.is_(None))
    )).scalar_one())
    active_membership_count = int((await db.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.revoked_at.is_(None))
    )).scalar_one())
    # Pending = not accepted, not revoked, not expired.
    pending_invitation_count = int((await db.execute(
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.accepted_at.is_(None))
        .where(Invitation.revoked_at.is_(None))
        .where(Invitation.expires_at > now)
    )).scalar_one())

    spend_by_user = await month_to_date_spend_by_user(db, now=now)
    total_spend = round(sum(spend_by_user.values()), 6)

    return AdminOverview(
        user_count=user_count,
        admin_count=admin_count,
        disabled_user_count=disabled_count,
        record_count=record_count,
        active_membership_count=active_membership_count,
        pending_invitation_count=pending_invitation_count,
        month_to_date_spend_usd=total_spend,
    )
