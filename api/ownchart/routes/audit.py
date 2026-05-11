"""Audit log endpoint — surface every Anthropic call.

Critical to the doctrine: "AI output is never silent truth; it is a cited,
reviewable fact." That requires the user be able to see exactly what was
sent and when. This endpoint exposes the ModelRun audit table.

PHI safety: ModelRun fields don't carry the prompt text or response text
themselves — only purpose, model, prompt_version, hashes, usage, and any
error string. The hashes can be cross-checked against the prompt YAML in
the repo.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..models.model_run import ModelRun
from ..models.user import User
from .auth import get_current_user

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
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[ModelRunReadout]:
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
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ModelRunReadout:
    r = await db.get(ModelRun, model_run_id)
    if r is None:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)
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
