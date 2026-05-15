"""/api/ask — natural-language query over the longitudinal record.

V1 retrieval is pg_trgm-based (fuzzy substring match across fact
labels + descriptions). Embedding-driven semantic retrieval is a V1.1
follow-up — we only need decent recall to populate the dossier
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

from datetime import datetime, timezone

from ..core.consent import require_phi_consent
from ..core.db import get_session
from ..llm import call_with_tool, get_registry
from ..models.conversation import Conversation, ConversationMessage
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
    # Persisted Conversation so the user can "Save as Dossier" /
    # "Continue in chat" from the Ask page. None when the answer was
    # blocked (e.g., self-harm guard).
    conversation_id: str | None = None


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

    # 40 was the original search_facts default; ask.py used to override
    # to 24 for prompt-context economy. After the 2026-05-13 ordering fix
    # the cap rarely truncates anything load-bearing, but giving the LLM
    # a bit more headroom on "tell me the story of X" queries is cheap
    # and reduces "I don't see X in your record" misses where X is real.
    # user_id makes search_facts pattern-aware (re-include facts
    # suppressed via accepted medication/provider pattern compression).
    facts = await search_facts(db, body.question, limit=40, user_id=user.id)
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
    answer_text = out.get("answer")

    # Persist the Q+A as a Conversation so the user can Save-as-Dossier
    # or continue the thread in /chat. Skipped when the model refused
    # or gave us nothing to save. Kind='ask' is the existing default —
    # the conversation list page distinguishes /ask-originated threads
    # from chat-originated ones via this field.
    conv_id_out: str | None = None
    if answer_text:
        now = datetime.now(timezone.utc)
        title = (body.question.strip().splitlines()[0] or "Ask")[:200]
        conv = Conversation(
            user_id=user.id,
            title=title,
            kind="ask",
            scope={"type": "whole_record"},
            last_message_at=now,
        )
        db.add(conv)
        await db.flush()
        db.add(ConversationMessage(
            conversation_id=conv.id,
            user_id=user.id,
            role="user",
            content=body.question,
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            content=answer_text,
            model_run_id=result.model_run_id,
        ))
        await db.commit()
        conv_id_out = str(conv.id)

    return AskResponse(
        question=body.question,
        answer=answer_text,
        well_supported=out.get("well_supported", []),
        uncertain=out.get("uncertain", []),
        suggested_next_steps=out.get("suggested_next_steps", []),
        citations=[Citation(**c) for c in out.get("citations", [])],
        retrieved_fact_count=len(facts),
        model_run_id=str(result.model_run_id),
        safety_response=out.get("safety_response"),
        error=result.error,
        conversation_id=conv_id_out,
    )
