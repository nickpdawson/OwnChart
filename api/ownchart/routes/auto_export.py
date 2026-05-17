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

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from ..core.arq_pool import enqueue_auto_export_processing
from ..core.auth_context import AuthContext, get_auth_context
from ..core.auto_export_auth import authenticate_auto_export_push
from ..core.config import get_settings
from ..core.db import SessionLocal
from ..core.logger import get_logger
from ..ingest import storage
from ..models.source_document import SourceDocument

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
    _ctx: AuthContext = Depends(get_auth_context),
) -> PushConfigReadout:
    """Return the URL + token to paste into the iOS app's REST API setup.

    Session-authed (any active membership). The token is shown in
    full because the iOS app needs it once; rotating the token
    requires editing `infra/.env` on the server, so we don't
    surface a rotate button in V1.

    M02 perimeter (Batch 8): we surface the LEGACY env token here.
    A future iteration will surface per-record tokens via Settings
    → Auto Export so multi-record instances aren't forced to rely
    on the env-fallback path.
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


@router.post("/push", response_model=PushReceipt, status_code=status.HTTP_202_ACCEPTED)
async def push_auto_export(
    request: Request,
    authorization: str | None = Header(default=None),
) -> PushReceipt:
    """Accept a Health Auto Export JSON push.

    Configure the iOS app's Automations → REST API tab with:
      URL:        https://<your-host>/api/auto-export/push
      Method:     POST
      Auth:       Bearer <token>
      Schedule:   anything sensible — typically every few hours, or
                  after each workout/sleep session.

    The app sends the same JSON shape as a manual file export, but
    typically only the data added since the last push. Incremental
    or full payloads are both accepted; we don't dedupe at the push
    level for V1 (#32 / multi-source collapse handles display dedup).

    M02 perimeter (Batch 8): bearer auth is resolved via
    `authenticate_auto_export_push` (core/auto_export_auth.py).
    Two paths:

      1. **Per-(user, record) token** (preferred): the token row
         identifies both the actor and the destination record. The
         resulting SourceDocument binds to `result.person_record_id`
         — NOT to any caller-controlled value (the iOS app cannot
         override which record the data lands on by adding a header).

      2. **Legacy env token** (`OWNCHART_AUTO_EXPORT_TOKEN`): only
         valid when the instance has exactly ONE active
         person_record. Multi-record instances must issue per-record
         tokens; the env fallback raises 503 with a clear message.

    Either way, the destination record id flows from the token
    auth, not from the request — there is no way to misroute.
    """
    # M02 perimeter: authenticate BEFORE touching storage. A bogus
    # or missing bearer must not cost us a write to the evidence
    # vault, and (more important) we must not buffer the raw push
    # body without knowing whose record it's destined for.
    async with SessionLocal() as db:
        # PM A-2: per-(user, record) token resolves both the
        # actor AND the destination record. Legacy env token only
        # works on single-record instances.
        auth_result = await authenticate_auto_export_push(
            db, authorization_header=authorization,
        )

        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")

        # Persist the raw bytes immediately and create a SourceDocument
        # in 'pending' state. Parsing happens in the worker so the iOS
        # app gets a fast 202 and doesn't time out on big pushes.
        async def _stream():
            yield raw

        blob = await storage.write_blob(_stream(), suffix=".json")

        ts = datetime.now(timezone.utc)
        src_id = uuid.uuid4()
        src = SourceDocument(
            id=src_id,
            owner_user_id=auth_result.user.id,
            person_record_id=auth_result.person_record_id,
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
                # Audit-trail for "which auth path did this push
                # take?" — helps support diagnose "I rotated the
                # env token and pushes still work" (= per-record
                # token covered it).
                "auth_method": auth_result.auth_method,
                "auth_token_id": (
                    str(auth_result.token_id)
                    if auth_result.token_id else None
                ),
            },
        )
        db.add(src)
        await db.commit()

    arq_id = await enqueue_auto_export_processing(str(src_id))

    log.info(
        "auto_export_push_accepted",
        source_id=str(src_id),
        person_record_id=str(auth_result.person_record_id),
        auth_method=auth_result.auth_method,
        bytes=len(raw),
        arq_job_id=arq_id,
    )
    return PushReceipt(
        source_id=str(src_id),
        bytes_received=len(raw),
        processing_status="pending",
    )
