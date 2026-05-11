from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..models.user import User
from .auth import get_current_user

router = APIRouter()


class ConsentState(BaseModel):
    granted: bool


@router.get("")
async def get_consent(user: User = Depends(get_current_user)) -> ConsentState:
    return ConsentState(granted=user.phi_consent_granted)


@router.put("")
async def set_consent(
    body: ConsentState,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConsentState:
    user.phi_consent_granted = body.granted
    await db.commit()
    return ConsentState(granted=user.phi_consent_granted)
