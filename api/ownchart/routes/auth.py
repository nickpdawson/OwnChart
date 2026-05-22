import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.app_config import get_app_config
from ..core.auth_context import (
    HEADER_PERSON_RECORD,
    SESSION_KEY_ACTIVE_RECORD,
    _parse_record_id_from_header,
    _parse_session_active_record,
    _read_session_payload,
    resolve_active_record_id,
)
from ..core.config import get_settings
from ..core.db import get_session
from ..core.security import hash_password, sign_session, verify_password
from ..models.membership import Membership
from ..models.person_record import PersonRecord
from ..models.user import User

router = APIRouter()
_settings = get_settings()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(LoginRequest):
    pass


class MembershipOut(BaseModel):
    """One row in the user's memberships list — what records they
    can access and at what role."""
    person_record_id: str
    role: str
    display_name: str
    is_self: bool


class ActiveRecordOut(BaseModel):
    """The currently-resolved active record for this request.
    Null if the user has zero memberships OR their requested
    record can't be resolved (in which case the client should
    pick from `memberships[]`)."""
    id: str
    display_name: str
    role: str


class SetActiveRecordRequest(BaseModel):
    """Body of `POST /api/auth/set-active-record`. The web record
    switcher pins the user's choice into the session cookie so
    subsequent requests resolve to it without re-sending a header.

    iOS does NOT use this endpoint — it sends
    `X-OwnChart-Person-Record` on every request and never relies
    on session pinning. See `core/auth_context.py` for the
    four-step resolution order."""
    person_record_id: str


class MeResponse(BaseModel):
    """Bootstrap response for any signed-in client.

    PM Decision Note §1 (2026-05-17): `/api/auth/me` is the
    endpoint web + iOS clients use to learn the user's identity,
    their full set of person-record memberships, the currently
    resolved active record (if any), and instance-admin flag.
    Distinct from record-scoped routes: `/me` does NOT require
    membership — a user with zero memberships still gets a 200
    so the client can show "no records — contact admin" UI.

    Backward-compatible: pre-M02 iOS builds reading `id`, `email`,
    `phi_consent_granted` get those fields unchanged. New fields
    are additive."""
    id: str
    email: EmailStr
    phi_consent_granted: bool
    # M02 additions (PM Decision Note §1):
    is_instance_admin: bool = False
    default_person_record_id: str | None = None
    memberships: list[MembershipOut] = []
    active_record: ActiveRecordOut | None = None


def _compose_session_payload(
    *,
    user_id: str,
    active_record_id: str | None = None,
) -> dict:
    """Build the dict that gets signed into the session cookie.

    Pulled out as a pure function so the switcher can pin the
    `{uid, active_record_id}` shape without spinning up FastAPI.
    The active_record_id key only appears when set — pre-M02 iOS
    builds and fresh logins keep the legacy `{uid}` shape.
    """
    payload: dict = {"uid": user_id}
    if active_record_id is not None:
        payload[SESSION_KEY_ACTIVE_RECORD] = active_record_id
    return payload


def _set_session_cookie(
    response: Response,
    user_id: str,
    *,
    active_record_id: str | None = None,
) -> None:
    token = sign_session(
        _compose_session_payload(
            user_id=user_id, active_record_id=active_record_id,
        )
    )
    response.set_cookie(
        key=_settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=_settings.env != "dev",
        samesite="lax",
        max_age=_settings.session_max_age_seconds,
        path="/",
    )


# `get_current_user` is the dual-mode dependency: accepts either an
# `Authorization: Bearer <device-token>` header (native iOS app) OR the
# `ownchart_session` cookie (web). Aliasing here means every existing
# `Depends(get_current_user)` call site picks up bearer support
# automatically — no per-route edits needed.
from ..core.device_auth import (  # noqa: E402
    get_user_from_device_token_or_session as get_current_user,
)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Self-registration endpoint.

    Two gates, per PM A-6 (2026-05-17):

      1. **Fresh DB** (no users yet) — accept unconditionally and
         flag the new user as `is_instance_admin=True`. This is the
         "first user creates owner" path; matches today's behavior
         and is independent of the `allow_self_registration` flag.
      2. **Any user already exists** — gate by
         `auth.allow_self_registration` from `infra/config.yaml`
         (default `false`). When `true`, family members can
         register their own logins; new accounts get no
         auto-membership (admin/owner adds them via the membership
         flow once `/api/person-records/members` lands).

    The flag wire-up resolves the docs-site M01 blocker:
    `auth.allow_self_registration` was declared but unread until
    now. PM resolution promotes it from dead config to live gate.
    """
    existing_first = (
        await db.execute(select(User).limit(1))
    ).scalars().first()
    is_first_user = existing_first is None

    if not is_first_user:
        # Subsequent signups need the operator to have opted in.
        cfg = get_app_config()
        if not cfg.auth.allow_self_registration:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Self-registration is closed on this instance. "
                    "Ask your admin to add you, or set "
                    "auth.allow_self_registration in config.yaml."
                ),
            )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        is_instance_admin=is_first_user,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    _set_session_cookie(response, str(user.id))
    return MeResponse(id=str(user.id), email=user.email, phi_consent_granted=user.phi_consent_granted)


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> MeResponse:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    _set_session_cookie(response, str(user.id))
    return MeResponse(id=str(user.id), email=user.email, phi_consent_granted=user.phi_consent_granted)


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(_settings.session_cookie_name, path="/")
    return {"ok": True}


async def _load_active_memberships(
    db: AsyncSession, user_id: uuid.UUID,
) -> list[tuple[Membership, PersonRecord]]:
    """All active (non-revoked) memberships joined to their records.

    Ordered (created_at ASC, id ASC) so the first-membership
    fallback in `resolve_active_record_id` is deterministic — same
    user gets the same default record on every cold request.
    """
    rows = (await db.execute(
        select(Membership, PersonRecord)
        .join(PersonRecord, PersonRecord.id == Membership.person_record_id)
        .where(Membership.user_id == user_id)
        .where(Membership.revoked_at.is_(None))
        .where(PersonRecord.disconnected_at.is_(None))
        .order_by(Membership.created_at.asc(), Membership.id.asc())
    )).all()
    return list(rows)


def _compose_me_response(
    *,
    user: User,
    memberships: list[tuple[Membership, PersonRecord]],
    active_record_id: uuid.UUID | None,
) -> MeResponse:
    """Pure-function MeResponse composer.

    Pulled out for direct unit testing without setting up a DB
    fixture. `memberships` is the already-loaded list; the helper
    looks up the active record + role within that list rather than
    re-querying.
    """
    by_record: dict[uuid.UUID, tuple[Membership, PersonRecord]] = {
        rec.id: (mem, rec) for mem, rec in memberships
    }
    memberships_out = [
        MembershipOut(
            person_record_id=str(rec.id),
            role=mem.role,
            display_name=rec.display_name,
            is_self=rec.is_self,
        )
        for mem, rec in memberships
    ]
    active_out: ActiveRecordOut | None = None
    if active_record_id is not None and active_record_id in by_record:
        mem, rec = by_record[active_record_id]
        active_out = ActiveRecordOut(
            id=str(rec.id),
            display_name=rec.display_name,
            role=mem.role,
        )
    return MeResponse(
        id=str(user.id),
        email=user.email,
        phi_consent_granted=user.phi_consent_granted,
        is_instance_admin=user.is_instance_admin,
        default_person_record_id=(
            str(user.default_person_record_id)
            if user.default_person_record_id else None
        ),
        memberships=memberships_out,
        active_record=active_out,
    )


@router.get("/me")
async def me(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> MeResponse:
    """V2 shape (PM Decision Note §1, 2026-05-17): returns user
    identity + memberships + active record.

    Does NOT require AuthContext. A user with zero memberships
    sees `memberships=[]` and `active_record=null`; their client
    routes to "no records — contact admin" UI. This makes /me
    the bootstrap endpoint that every signed-in client can call
    safely.

    Active-record resolution uses the same 4-step algorithm as
    `get_auth_context` (header → session → default → first), but
    returns `null` on miss instead of raising. Cross-record leak
    protection: only the calling user's memberships are returned;
    no other user's records appear.
    """
    memberships = await _load_active_memberships(db, user.id)
    active_ids = [rec.id for _, rec in memberships]

    header_record = _parse_record_id_from_header(
        request.headers.get(HEADER_PERSON_RECORD),
    )
    session_pin = _parse_session_active_record(
        _read_session_payload(request),
    )
    resolved, _reason = resolve_active_record_id(
        header_record_id=header_record,
        session_pin=session_pin,
        default_record_id=user.default_person_record_id,
        active_membership_record_ids=active_ids,
        user_explicitly_requested_record=(
            header_record is not None or session_pin is not None
        ),
    )
    # On /me we intentionally do NOT 403 on `denied`. The client
    # asked for THEIR own bootstrap state; returning active_record=null
    # lets them learn they need to pick a different record.
    return _compose_me_response(
        user=user,
        memberships=memberships,
        active_record_id=resolved,
    )


# ---------------------------------------------------------------------------
# Web record switcher (Beta 1 Section B — Multi-tenant UI)


def _parse_target_record_id(raw: str) -> uuid.UUID | None:
    """Best-effort UUID parse for the switcher payload. Returns
    None for any malformed input; the route maps that to a 404
    so we do not leak server validation rules. Matches the
    header-parsing pattern in `core/auth_context.py`."""
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _classify_switch_target(
    *,
    target_id: uuid.UUID,
    active_memberships: list[tuple[Membership, PersonRecord]],
    has_any_membership_row: bool,
) -> Literal["ok", "revoked", "not_found"]:
    """Pure-function classifier for the record-switcher.

    Inputs are two facts the caller has already queried:
      - `active_memberships` — non-revoked memberships joined to
        non-disconnected records (the canonical happy-path set).
      - `has_any_membership_row` — does the membership table have
        ANY row (revoked or not) for this user+record? Distinguishes
        "you used to have access, it was revoked" from "you never
        had access / record doesn't exist".

    Returns one of:
      - "ok": the target is in the user's active membership set;
        the switch is allowed.
      - "revoked": user had a membership row but it's not active
        (revoked, or the underlying record is disconnected).
      - "not_found": no membership row at all — either the record
        does not exist, or it exists but belongs only to other
        users. Same response to avoid leaking record existence.
    """
    for _mem, rec in active_memberships:
        if rec.id == target_id:
            return "ok"
    if has_any_membership_row:
        return "revoked"
    return "not_found"


@router.post("/set-active-record")
async def set_active_record(
    body: SetActiveRecordRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Pin the active person_record for this web session.

    The web record switcher calls this when the user picks a
    different record from the sidebar dropdown. The endpoint:

      1. Validates that the user has a non-revoked membership on
         the target record (404 if no membership row exists at all;
         403 record_access_revoked if a row exists but was revoked
         or the underlying record is disconnected).
      2. Re-signs the session cookie with
         `{uid, active_record_id}` so every subsequent request
         resolves to the new record without needing the header.
      3. Returns the updated `MeResponse` so the client can hydrate
         the new active record immediately without a follow-up
         /me round-trip.

    Cross-record leak guard: the validation step only consults the
    caller's own memberships. A user cannot switch to a record
    they don't have access to, regardless of what id they POST.
    """
    target_id = _parse_target_record_id(body.person_record_id)
    if target_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "record_not_found",
                "message": "Record not found.",
            },
        )

    memberships = await _load_active_memberships(db, user.id)

    # Second query: does ANY membership row (revoked or not) link
    # this user to this record? Distinguishes revoked-from-never.
    any_row = (await db.execute(
        select(Membership)
        .where(Membership.user_id == user.id)
        .where(Membership.person_record_id == target_id)
        .limit(1)
    )).scalar_one_or_none()

    verdict = _classify_switch_target(
        target_id=target_id,
        active_memberships=memberships,
        has_any_membership_row=any_row is not None,
    )

    if verdict == "revoked":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "record_access_revoked",
                "message": (
                    "Your access to this record has been revoked. "
                    "Refresh memberships and pick another record."
                ),
            },
        )
    if verdict == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "record_not_found",
                "message": "Record not found.",
            },
        )

    _set_session_cookie(
        response,
        str(user.id),
        active_record_id=str(target_id),
    )
    return _compose_me_response(
        user=user,
        memberships=memberships,
        active_record_id=target_id,
    )
