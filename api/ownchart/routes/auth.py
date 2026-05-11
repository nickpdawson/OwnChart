from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.db import get_session
from ..core.security import hash_password, sign_session, verify_password
from ..models.user import User

router = APIRouter()
_settings = get_settings()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(LoginRequest):
    pass


class MeResponse(BaseModel):
    id: str
    email: EmailStr
    phi_consent_granted: bool


def _set_session_cookie(response: Response, user_id: str) -> None:
    token = sign_session({"uid": user_id})
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
    """V1 single-tenant. After the first owner is created, /register is closed."""
    existing_count = (await db.execute(select(User))).scalars().first()
    if existing_count is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner account already exists; registration is closed.",
        )
    user = User(email=body.email, password_hash=hash_password(body.password))
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


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=str(user.id), email=user.email, phi_consent_granted=user.phi_consent_granted)
