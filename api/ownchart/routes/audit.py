"""Audit log endpoint — surface every Anthropic call.

Critical to the doctrine: "AI output is never silent truth; it is a cited,
reviewable fact." That requires the user be able to see exactly what was
sent and when. This endpoint exposes the ModelRun audit table.

PHI safety: ModelRun fields don't carry the prompt text or response text
themselves — only purpose, model, prompt_version, hashes, usage, and any
error string. The hashes can be cross-checked against the prompt YAML in
the repo.

M02 Slice 1 Batch 9 perimeter note: ModelRun is a SYSTEM audit catalog
with no person_record_id, so it is not record-scoped. Per PM's
"instance/admin/system audit views may remain admin/global where
appropriate" allowlist, both handlers below are gated to
``is_instance_admin``. Per-user, per-turn audit needs are already met by
ConversationCitation, BriefMessage.citations, and the Ask response
citation shape — those surfaces stay record-scoped via AuthContext.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, get_auth_context
from ..core.db import get_session
from ..models.model_run import ModelRun


def _require_instance_admin(ctx: AuthContext) -> None:
    """ModelRun is a SYSTEM audit catalog (M02 Slice 1 Batch 9
    design decision). It carries no person_record_id and is
    intentionally not record-scoped — gating to is_instance_admin
    keeps it admin-global per PM's 'instance/admin/system audit
    views may remain admin/global where appropriate' allowlist.

    Per-user audit needs (which call did the LLM make to answer
    this question?) are already met by the per-turn citation rows
    on ConversationCitation, BriefMessage.citations, and the Ask
    response shape. This catalog is for the operator.
    """
    if not ctx.user.is_instance_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "instance_admin_required",
                "message": (
                    "The model-run audit catalog is an instance-admin "
                    "surface. Per-conversation citations are surfaced "
                    "on each thread."
                ),
            },
        )


router = APIRouter()


class ModelRunReadout(BaseModel):
    id: str
    provider: str
    model: str
    purpose: str
    prompt_version: str
    consent_state: bool
    input_source_ids: list[str]
    input_hash: str | None
    output_hash: str | None
    usage: dict | None
    error: str | None
    created_at: datetime


@router.get("/model-runs")
async def list_model_runs(
    purpose: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> list[ModelRunReadout]:
    _require_instance_admin(ctx)
    stmt = select(ModelRun).order_by(ModelRun.created_at.desc()).limit(limit)
    if purpose:
        stmt = stmt.where(ModelRun.purpose == purpose)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        ModelRunReadout(
            id=str(r.id),
            provider=r.provider,
            model=r.model,
            purpose=r.purpose,
            prompt_version=r.prompt_version,
            consent_state=r.consent_state,
            input_source_ids=[str(s) for s in (r.input_source_ids or [])],
            input_hash=r.input_hash,
            output_hash=r.output_hash,
            usage=r.usage,
            error=r.error,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/model-runs/{model_run_id}")
async def get_model_run(
    model_run_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> ModelRunReadout:
    _require_instance_admin(ctx)
    r = await db.get(ModelRun, model_run_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return ModelRunReadout(
        id=str(r.id),
        provider=r.provider,
        model=r.model,
        purpose=r.purpose,
        prompt_version=r.prompt_version,
        consent_state=r.consent_state,
        input_source_ids=[str(s) for s in (r.input_source_ids or [])],
        input_hash=r.input_hash,
        output_hash=r.output_hash,
        usage=r.usage,
        error=r.error,
        created_at=r.created_at,
    )
