"""Health Auto Export REST push endpoint.

The Lybrary Health Auto Export iOS app supports POSTing exports to a
configured URL with an optional Authorization header
(https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/).
This is the right ingest path for ongoing wearable data — incremental,
automated, no manual file upload — far better UX than re-exporting
the whole history every time.

Auth model (V1 single-tenant):
  - A single deployment-wide bearer token in `OWNCHART_AUTO_EXPORT_TOKEN`.
  - All pushes resolve to the owner account.
  - Multi-user deploys will want per-user push tokens; that's a V1.1
    schema addition (push_tokens table with FK to users).

Async-by-default: the iOS app has a short request timeout (~30–60s)
and an 8 MB push with ~40k facts can take 15+ seconds to parse +
write. Returning fast prevents the iOS app from giving up and
retrying the same payload. So the push endpoint:

  1. Authenticates and writes the raw bytes to storage (small, fast).
  2. Creates a SourceDocument with raw_metadata.processing_status=
     'pending'.
  3. Enqueues the Arq task `process_auto_export_push(source_id)`.
  4. Returns 202 immediately with the source_id.

The worker reads the saved JSON, parses, and creates facts/anchors
in batches.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.arq_pool import enqueue_auto_export_processing
from ..core.config import get_settings
from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..ingest import storage
from ..models.source_document import SourceDocument
from ..models.user import User
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.auto_export")


class PushReceipt(BaseModel):
    source_id: str
    bytes_received: int
    processing_status: str  # 'pending' (queued for the worker)


class PushConfigReadout(BaseModel):
    push_url: str
    token: str | None
    configured: bool


@router.get("/config", response_model=PushConfigReadout)
async def get_push_config(
    _user: User = Depends(get_current_user),
) -> PushConfigReadout:
    """Return the URL + token to paste into the iOS app's REST API setup.

    Owner-only (session-authed). The token is shown in full because
    the iOS app needs it once; rotating the token requires editing
    `infra/.env` on the server, so we don't surface a rotate button
    in V1.
    """
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    push_url = f"{base}/api/auto-export/push"
    tok = settings.auto_export_token.get_secret_value() if settings.auto_export_token else None
    return PushConfigReadout(
        push_url=push_url,
        token=tok,
        configured=bool(tok),
    )


def _check_bearer(authorization: str | None) -> None:
    """Constant-time compare against the configured push token."""
    settings = get_settings()
    expected = settings.auto_export_token
    if expected is None or not expected.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OWNCHART_AUTO_EXPORT_TOKEN is not configured on the server. "
                "The push endpoint is closed until the deployer sets it."
            ),
        )
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be `Bearer <token>`",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )
    presented = parts[1].strip()
    if not secrets.compare_digest(presented, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization token",
            headers={"WWW-Authenticate": 'Bearer realm="auto-export"'},
        )


async def _resolve_owner(db: AsyncSession) -> User:
    """V1 single-tenant: owner is the first/only user.

    A multi-user deploy would resolve via a per-user token table
    instead (push_tokens.user_id).
    """
    user = (await db.execute(select(User).order_by(User.created_at).limit(1))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No owner account exists yet — register first via /login.",
        )
    return user


@router.post("/push", response_model=PushReceipt, status_code=status.HTTP_202_ACCEPTED)
async def push_auto_export(
    request: Request,
    authorization: str | None = Header(default=None),
) -> PushReceipt:
    """Accept a Health Auto Export JSON push.

    Configure the iOS app's Automations → REST API tab with:
      URL:        https://<your-host>/api/auto-export/push
      Method:     POST
      Auth:       Bearer <OWNCHART_AUTO_EXPORT_TOKEN>
      Schedule:   anything sensible — typically every few hours, or
                  after each workout/sleep session.

    The app sends the same JSON shape as a manual file export, but
    typically only the data added since the last push. Incremental
    or full payloads are both accepted; we don't dedupe at the push
    level for V1 (#32 / multi-source collapse handles display dedup).
    """
    _check_bearer(authorization)
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")

    # Persist the raw bytes immediately and create a SourceDocument
    # in 'pending' state. Parsing happens in the worker so the iOS app
    # gets a fast 202 and doesn't time out on big pushes.
    async def _stream():
        yield raw

    blob = await storage.write_blob(_stream(), suffix=".json")

    async with SessionLocal() as db:
        user = await _resolve_owner(db)
        ts = datetime.now(timezone.utc)
        src_id = uuid.uuid4()
        src = SourceDocument(
            id=src_id,
            owner_user_id=user.id,
            source_type="auto_export",
            original_filename=f"auto-export-push-{ts.strftime('%Y%m%d-%H%M%S')}.json",
            storage_uri=blob.storage_uri,
            hash=f"sha256:{blob.sha256}",
            mime_type="application/json",
            acquired_at=ts,
            source_system="health_auto_export",
            source_label=f"Auto Export push {ts.strftime('%Y-%m-%d %H:%M')} UTC",
            raw_metadata={
                "transport": "rest_push",
                "deduplicated": blob.already_existed,
                "size_bytes": blob.size_bytes,
                "processing_status": "pending",
                "processing_enqueued_at": ts.isoformat(),
            },
        )
        db.add(src)
        await db.commit()

    arq_id = await enqueue_auto_export_processing(str(src_id))

    log.info(
        "auto_export_push_accepted",
        source_id=str(src_id),
        bytes=len(raw),
        arq_job_id=arq_id,
    )
    return PushReceipt(
        source_id=str(src_id),
        bytes_received=len(raw),
        processing_status="pending",
    )
