"""GET /api/model-runs/receipts — the iOS "Data Route receipt".

Powers the post-action receipt from the 2026-07-20 review's P0 "Data
Route receipt" feature: after an AI action the app shows what actually
left the device — which model, which prompt version, what it cost,
which record it touched, and whether it succeeded.

``model_runs`` already carries everything (``user_id`` +
``person_record_id`` + ``estimated_cost_usd`` since migration 0053), so
this is a read-only projection — no new table, per the stateless-echo
doctrine.

Scoping (the MED-5 lesson): rows are filtered STRICTLY to
``ModelRun.user_id == caller``. A second user's activity is never
returned here, full stop. In demo mode the shared ``demo@ownchart.me``
account would otherwise leak every visitor's runs, so demo callers are
further narrowed to the runs reachable from THEIR OWN
``oc_demo_session``-scoped conversations — the same per-visitor
isolation the conversation list enforces.

Auth-required, no consent gate (metadata, not an AI call).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth_context import AuthContext, get_auth_context
from ..core.config import get_settings
from ..core.db import get_session
from ..core.demo_session import get_demo_session_id
from ..models.conversation import Conversation, ConversationMessage
from ..models.model_run import ModelRun

router = APIRouter()


def receipt_status(error: str | None) -> str:
    """Derive a receipt status from a ModelRun's error column.

    ``ok`` when the run completed; ``error`` when it recorded a failure.
    Pure — the raw error string is never surfaced (it can echo prompt
    fragments); the receipt carries only this coarse status.
    """
    return "error" if error else "ok"


class Receipt(BaseModel):
    id: str
    created_at: datetime
    purpose: str
    provider: str
    model: str
    prompt_version: str | None
    estimated_cost_usd: float | None
    person_record_id: str | None
    status: str


class ReceiptsResponse(BaseModel):
    receipts: list[Receipt]
    limit: int
    count: int


@router.get("/receipts", response_model=ReceiptsResponse)
async def get_receipts(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> ReceiptsResponse:
    """Recent model runs for the CALLING user, newest first.

    MED-5: scoped to ``ModelRun.user_id == caller``. In demo mode the
    result is additionally restricted to runs cited by this visitor's
    own conversations so the shared demo account can't leak one
    visitor's AI activity to the next.
    """
    user = ctx.user

    stmt = (
        select(ModelRun)
        .where(ModelRun.user_id == user.id)
        .order_by(ModelRun.created_at.desc())
        .limit(limit)
    )

    if get_settings().demo_mode:
        sid = get_demo_session_id(request)
        if not sid:
            # First request from this visitor — no cookie on the wire
            # yet. Return nothing rather than the shared pool.
            return ReceiptsResponse(receipts=[], limit=limit, count=0)
        # Narrow to model runs referenced by THIS visitor's
        # conversations (scope->>demo_session_id matches the cookie).
        visitor_run_ids = (
            select(ConversationMessage.model_run_id)
            .join(
                Conversation,
                Conversation.id == ConversationMessage.conversation_id,
            )
            .where(Conversation.user_id == user.id)
            .where(ConversationMessage.model_run_id.is_not(None))
            .where(
                text("conversations.scope->>'demo_session_id' = :dsid")
                .bindparams(dsid=sid)
            )
        )
        stmt = stmt.where(ModelRun.id.in_(visitor_run_ids))

    rows = list((await db.execute(stmt)).scalars().all())
    receipts = [
        Receipt(
            id=str(r.id),
            created_at=r.created_at,
            purpose=r.purpose,
            provider=r.provider,
            model=r.model,
            prompt_version=r.prompt_version,
            estimated_cost_usd=(
                float(r.estimated_cost_usd)
                if r.estimated_cost_usd is not None
                else None
            ),
            person_record_id=(
                str(r.person_record_id)
                if r.person_record_id is not None
                else None
            ),
            status=receipt_status(r.error),
        )
        for r in rows
    ]
    return ReceiptsResponse(receipts=receipts, limit=limit, count=len(receipts))
