"""Settings API — docs/09.

Three endpoints carry V1:

  GET  /api/settings/registry   — the schema (drives the UI form)
  GET  /api/settings/effective  — current values for this user
  PATCH /api/settings/user      — write a user-scope override

Every write is audited (audit_events row). Admin-only / instance-
scope endpoints are out of scope for V1 (Nick is single-user); the
shape is ready for them later.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..models.audit_event import AuditEvent
from ..models.user import User
from ..models.user_setting import UserSetting
from ..settings.registry import SettingsError, coerce_value, effective_all, get_registry
from .auth import get_current_user

router = APIRouter()


class SettingShape(BaseModel):
    key: str
    label: str
    description: str
    section: str
    scope: str
    storage: str
    type: str
    default: Any
    choices: list[Any] = Field(default_factory=list)
    ui_writable: bool
    file_writable: bool
    requires_restart: bool
    phi_sensitive: bool
    admin_lockable: bool


class RegistryResponse(BaseModel):
    settings: list[SettingShape]


class EffectiveResponse(BaseModel):
    values: dict[str, Any]


class PatchUserRequest(BaseModel):
    key: str
    value: Any


class PatchUserResponse(BaseModel):
    key: str
    value: Any


@router.get("/registry", response_model=RegistryResponse)
async def get_settings_registry(
    _user: User = Depends(get_current_user),
) -> RegistryResponse:
    return RegistryResponse(
        settings=[SettingShape(**s.model_dump()) for s in get_registry().all()]
    )


@router.get("/effective", response_model=EffectiveResponse)
async def get_effective_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> EffectiveResponse:
    values = await effective_all(db, user)
    return EffectiveResponse(values=values)


@router.patch("/user", response_model=PatchUserResponse)
async def patch_user_setting(
    body: PatchUserRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> PatchUserResponse:
    try:
        setting = get_registry().get(body.key)
    except SettingsError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    if not setting.ui_writable:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"{body.key} is not UI-writable")
    if setting.scope != "user":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{body.key} has scope={setting.scope}; use the scope-specific endpoint",
        )
    try:
        coerced = coerce_value(setting, body.value)
    except SettingsError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    prev = (await db.execute(
        select(UserSetting)
        .where(UserSetting.user_id == user.id)
        .where(UserSetting.key == body.key)
    )).scalar_one_or_none()
    prev_value = prev.value if prev else setting.default

    stmt = (
        pg_insert(UserSetting)
        .values(user_id=user.id, key=body.key, value=coerced)
        .on_conflict_do_update(
            index_elements=["user_id", "key"],
            set_={"value": coerced, "updated_at": datetime.now(timezone.utc)},
        )
    )
    await db.execute(stmt)

    db.add(AuditEvent(
        user_id=user.id,
        event_type="setting_change",
        subject_type="setting",
        subject_id=body.key,
        detail={
            "from": prev_value,
            "to": coerced,
            "phi_sensitive": setting.phi_sensitive,
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    ))
    await db.commit()

    return PatchUserResponse(key=body.key, value=coerced)
