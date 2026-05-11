"""Instance metadata + demo-mode info.

Public endpoint (no auth required) so the web client can render the
"Demo · sample data" banner before login. Returns only non-secret
deployment-level info — never per-user state.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.config import get_settings

router = APIRouter()


class InstanceInfo(BaseModel):
    demo_mode: bool
    public_base_url: str
    # Surface the demo user only when demo mode is on so a public
    # landing page can pre-fill the login form. Never include the
    # password — the client knows it (it's in the repo) and the
    # server doesn't need to advertise it.
    demo_user_email: str | None = None


@router.get("/info", response_model=InstanceInfo)
async def get_instance_info() -> InstanceInfo:
    s = get_settings()
    return InstanceInfo(
        demo_mode=s.demo_mode,
        public_base_url=s.public_base_url,
        demo_user_email=s.demo_user_email if s.demo_mode else None,
    )
