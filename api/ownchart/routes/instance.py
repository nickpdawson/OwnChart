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
    # When True alongside demo_mode, the operator has temporarily
    # widened the read-only allowlist so they can SMART-on-FHIR
    # connect an EHR sandbox or upload sample data through the UI.
    # Banner copy should reflect this so visitors aren't confused.
    demo_allow_ingest: bool = False
    public_base_url: str
    demo_user_email: str | None = None


@router.get("/info", response_model=InstanceInfo)
async def get_instance_info() -> InstanceInfo:
    s = get_settings()
    return InstanceInfo(
        demo_mode=s.demo_mode,
        demo_allow_ingest=s.demo_mode and s.demo_allow_ingest,
        public_base_url=s.public_base_url,
        demo_user_email=s.demo_user_email if s.demo_mode else None,
    )
