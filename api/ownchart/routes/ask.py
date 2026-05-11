"""/api/ask — natural-language query over the longitudinal record.

V1 retrieval is pg_trgm-based (fuzzy substring match across fact
labels + descriptions). Embedding-driven semantic retrieval is a V1.1
follow-up — we only need decent recall to populate the strabismus
demo, and trigram does that with no additional infra.

Self-harm guard: input is keyword-screened; the system prompt also
binds the model to refuse via a `safety_response` field. Output is
rejected if it contains nothing else.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.consent import require_phi_consent
from ..core.db import get_session
from ..llm import call_with_tool, get_registry
from ..models.extracted_fact import ExtractedFact
from ..models.user import User
from ..retrieval.topics import search_facts
from .auth import get_current_user

router = APIRouter()


_SELF_HARM_PATTERNS = [
    r"kill myself",
    r"end my life",
    r"hurt myself",
    r"suicide",
    r"don'?t want to live",
]
_SELF_HARM_RESPONSE = (
    "I can't help with this kind of question. If you're in crisis, please contact "
    "a local crisis line — in the US you can call or text 988. If you're in immediate "
    "danger, call 911."
)


def _is_self_harm(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in _SELF_HARM_PATTERNS)


class AskRequest(BaseModel):
    question: str


class Citation(BaseModel):
    fact_id: str
    note: str | None = None


class AskResponse(BaseModel):
    question: str
    answer: str | None
    well_supported: list[str]
    uncertain: list[dict]
    suggested_next_steps: list[str]
    citations: list[Citation]
    retrieved_fact_count: int
    model_run_id: str | None
    safety_response: str | None
    error: str | None


def _format_context(facts: list[ExtractedFact]) -> str:
    if not facts:
        return "(no relevant facts found in your record)"
    lines = []
    for c in facts:
        date_str = c.date_start.date().isoformat() if c.date_start else "?"
        excerpt = (c.description or c.label)[:300]
        lines.append(
            f"- fact_id={c.id} type={c.fact_type} date={date_str} "
            f"review_state={c.review_state} confidence={c.confidence or '?'}\n"
            f"  label: {c.label}\n  excerpt: {excerpt}"
        )
    return "\n".join(lines)


@router.post("")
async def ask(
    body: AskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> AskResponse:
    if _is_self_harm(body.question):
        return AskResponse(
            question=body.question,
            answer=None,
            well_supported=[],
            uncertain=[],
            suggested_next_steps=[],
            citations=[],
            retrieved_fact_count=0,
            model_run_id=None,
            safety_response=_SELF_HARM_RESPONSE,
            error=None,
        )

    require_phi_consent(user)

    facts = await search_facts(db, body.question, limit=24)
    prompt = get_registry().get("ask_query")
    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "question": body.question,
            "context_block": _format_context(facts),
        },
        purpose="ask_query",
        tool_name="emit_answer",
    )

    if result.error and not result.tool_input:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM call failed: {result.error}",
        )

    out = result.tool_input or {}
    return AskResponse(
        question=body.question,
        answer=out.get("answer"),
        well_supported=out.get("well_supported", []),
        uncertain=out.get("uncertain", []),
        suggested_next_steps=out.get("suggested_next_steps", []),
        citations=[Citation(**c) for c in out.get("citations", [])],
        retrieved_fact_count=len(facts),
        model_run_id=str(result.model_run_id),
        safety_response=out.get("safety_response"),
        error=result.error,
    )
