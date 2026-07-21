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
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.arq_pool import enqueue_auto_export_processing
from ..core.auth_context import AuthContext, require_role
from ..core.auto_export_auth import (
    PUSH_SCOPE,
    authenticate_auto_export_push,
    generate_token,
    hash_token,
)
from ..core.config import get_settings
from ..core.db import SessionLocal, get_session
from ..core.logger import get_logger
from ..ingest import limits, storage
from ..models.auto_export_token import AutoExportToken
from ..models.source_document import SourceDocument

router = APIRouter()
log = get_logger("ownchart.routes.auto_export")


class PushReceipt(BaseModel):
    source_id: str
    bytes_received: int
    processing_status: str  # 'pending' (queued for the worker)


class PushConfigReadout(BaseModel):
    push_url: str
    # Whether a deployment-wide OWNCHART_AUTO_EXPORT_TOKEN is set. The
    # raw value is NEVER returned — an operator reads it from their own
    # infra/.env, or (preferred) mints a per-record token below.
    configured: bool
    # Masked tail of the env token so an owner can eyeball which token
    # is live without the value leaking. None when no env token is set.
    token_hint: str | None


@router.get("/config", response_model=PushConfigReadout)
async def get_push_config(
    _ctx: AuthContext = Depends(require_role("owner")),
) -> PushConfigReadout:
    """Return the push URL and whether a deployment token is configured.

    OWNER-ONLY. The deployment-wide `OWNCHART_AUTO_EXPORT_TOKEN` is a
    write credential for the record: anyone who reads it can POST
    arbitrary SourceDocuments + facts into the owner's record. It used
    to be returned in full to ANY active membership (including a
    `viewer`) — a viewer→writer escalation. We now (a) require the
    `owner` role and (b) never return the raw token, only a masked hint.
    To provision a device, mint a scoped per-record token via
    `POST /api/auto-export/tokens` (returned in plaintext exactly once).
    """
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    push_url = f"{base}/api/auto-export/push"
    tok = (
        settings.auto_export_token.get_secret_value()
        if settings.auto_export_token else None
    )
    hint = f"…{tok[-4:]}" if tok and len(tok) >= 4 else ("…" if tok else None)
    return PushConfigReadout(
        push_url=push_url,
        configured=bool(tok),
        token_hint=hint,
    )


# ---------------------------------------------------------------------------
# Per-(user, record) push tokens — the create/list/revoke surface the
# legacy env path was missing. Mirrors the device-token pattern
# (routes/auth_device.py): plaintext returned once, only the sha256
# hash stored.


class CreateTokenRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=128)
    # Forward-looking; Beta 1 only mints ['push']. Defaults there.
    scopes: list[str] | None = None


class CreatedTokenResponse(BaseModel):
    id: str
    label: str
    scopes: list[str]
    person_record_id: str
    # Plaintext token — shown EXACTLY ONCE at creation. Store it now.
    token: str


class TokenReadout(BaseModel):
    id: str
    label: str
    scopes: list[str]
    person_record_id: str
    created_at: datetime
    last_used_at: datetime | None


@router.post(
    "/tokens",
    response_model=CreatedTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_push_token(
    body: CreateTokenRequest,
    ctx: AuthContext = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_session),
) -> CreatedTokenResponse:
    """Issue a per-(user, active-record) Auto Export push token.

    OWNER-ONLY. The token binds the upload to `ctx.active_record_id`;
    the destination record flows from the token at push time, never
    from a caller-controlled header. Plaintext is returned once and
    only the sha256 hash is stored.
    """
    scopes = body.scopes or [PUSH_SCOPE]
    raw = generate_token()
    row = AutoExportToken(
        id=uuid.uuid4(),
        user_id=ctx.user.id,
        person_record_id=ctx.active_record_id,
        label=body.label.strip()[:128],
        token_hash=hash_token(raw),
        scopes=scopes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(
        "auto_export_token_created",
        token_id=str(row.id),
        user_id=str(ctx.user.id),
        person_record_id=str(ctx.active_record_id),
        scopes=scopes,
    )
    return CreatedTokenResponse(
        id=str(row.id),
        label=row.label,
        scopes=list(row.scopes),
        person_record_id=str(row.person_record_id),
        token=raw,
    )


@router.get("/tokens", response_model=list[TokenReadout])
async def list_push_tokens(
    ctx: AuthContext = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_session),
) -> list[TokenReadout]:
    """List active (non-revoked) push tokens for the active record."""
    rows = (await db.execute(
        select(AutoExportToken)
        .where(AutoExportToken.person_record_id == ctx.active_record_id)
        .where(AutoExportToken.revoked_at.is_(None))
        .order_by(AutoExportToken.created_at.desc())
    )).scalars().all()
    return [
        TokenReadout(
            id=str(r.id),
            label=r.label,
            scopes=list(r.scopes),
            person_record_id=str(r.person_record_id),
            created_at=r.created_at,
            last_used_at=r.last_used_at,
        )
        for r in rows
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_push_token(
    token_id: uuid.UUID,
    ctx: AuthContext = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Revoke a per-record push token. Scoped to the active record so an
    owner can only revoke tokens on the record they're acting on."""
    row = (await db.execute(
        select(AutoExportToken).where(
            AutoExportToken.id == token_id,
            AutoExportToken.person_record_id == ctx.active_record_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return None


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

        # Resource boundary (P0-5): fast Content-Length reject, then a
        # streaming byte-capped read of the raw body. Unlike `request.body()`
        # this aborts mid-stream so a runaway push never fully buffers.
        limits.reject_if_declared_length_over(request, limits.max_attachment_bytes())
        raw = await limits.read_body_capped(request)
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
