"""Instance metadata + demo-mode info.

Public endpoint (no auth required) so the web client can render the
"Demo · sample data" banner before login. Returns only non-secret
deployment-level info — never per-user state.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.app_config import get_app_config
from ..core.config import get_settings
from ..core.email import is_smtp_configured

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
    # Email/SMTP workstream — lets the client gate email-dependent
    # affordances. When `smtp_enabled` is False the "Forgot password?" link
    # and any "resend verification" UI should be hidden (the backend no-ops
    # those flows). `require_email_verification` tells the client whether a
    # freshly-registered user must verify before they can sign in.
    smtp_enabled: bool = False
    require_email_verification: bool = False


@router.get("/info", response_model=InstanceInfo)
async def get_instance_info() -> InstanceInfo:
    s = get_settings()
    cfg = get_app_config()
    smtp_enabled = is_smtp_configured()
    return InstanceInfo(
        demo_mode=s.demo_mode,
        demo_allow_ingest=s.demo_mode and s.demo_allow_ingest,
        public_base_url=s.public_base_url,
        demo_user_email=s.demo_user_email if s.demo_mode else None,
        smtp_enabled=smtp_enabled,
        # Only surface as required when it can actually be enforced (needs SMTP).
        require_email_verification=cfg.auth.require_email_verification and smtp_enabled,
    )
