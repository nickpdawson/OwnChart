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

from ..core.auth_context import AuthContext, get_auth_context
from ..core.consent import require_phi_consent
from ..core.db import get_session
from ..core.logger import get_logger
from ..llm import call_with_tool, get_registry
from ..models.conversation import Conversation, ConversationMessage
from ..models.extracted_fact import ExtractedFact
from ..retrieval.calendar_life_context import (
    fetch_calendar_life_context,
    format_calendar_context_block,
)
from ..retrieval.topics import search_facts

router = APIRouter()
log = get_logger("ownchart.routes.ask")


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
    ctx: AuthContext = Depends(get_auth_context),
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

    user = ctx.user
    require_phi_consent(user)

    # 40 was the original search_facts default; ask.py used to override
    # to 24 for prompt-context economy. After the 2026-05-13 ordering fix
    # the cap rarely truncates anything load-bearing, but giving the LLM
    # a bit more headroom on "tell me the story of X" queries is cheap
    # and reduces "I don't see X in your record" misses where X is real.
    # user_id makes search_facts pattern-aware (re-include facts
    # suppressed via accepted medication/provider pattern compression).
    #
    # M02 perimeter (Batch 4): person_record_id scopes every retrieval
    # pass (category, substring, source-name expansion) to the active
    # record. This is the load-bearing line for "Ask cannot leak
    # another record's facts into the prompt." Without it, a caregiver
    # asking about a parent's record could surface facts from their
    # own record (or vice versa).
    facts = await search_facts(
        db, body.question, limit=40,
        user_id=user.id,
        person_record_id=ctx.active_record_id,
    )
    retrieved_ids: set[str] = {str(f.id) for f in facts}

    # FU-CAL-ASK-INTEGRATION — append projected calendar life-context
    # to the prompt block. The projector enforces the two-elevation
    # floor (privacy_mode + llm_full_details_consent) per source and
    # the per-source history_window_back hides events that fall
    # outside the user's chosen back-window.
    calendar_items = await fetch_calendar_life_context(
        db, person_record_id=ctx.active_record_id,
    )
    fact_block = _format_context(facts)
    calendar_block = format_calendar_context_block(calendar_items)
    context_block = fact_block + calendar_block

    # Count-only retrieval-shape diagnostics (FU-CAL-ASK-INTEGRATION +
    # FU-ASK-RECENT-WEARABLE triage 2026-05-22). PM directive: never
    # log titles, ids, or any prompt body — only counts and the
    # boolean "block present" flags.
    fact_type_counts: dict[str, int] = {}
    extraction_method_counts: dict[str, int] = {}
    for f in facts:
        fact_type_counts[f.fact_type] = fact_type_counts.get(f.fact_type, 0) + 1
        em = f.extraction_method or "(none)"
        extraction_method_counts[em] = extraction_method_counts.get(em, 0) + 1
    log.info(
        "ask_retrieval_shape",
        person_record_id=str(ctx.active_record_id),
        fact_count=len(facts),
        fact_type_counts=fact_type_counts,
        extraction_method_counts=extraction_method_counts,
        calendar_item_count=len(calendar_items),
        fact_block_chars=len(fact_block),
        calendar_block_chars=len(calendar_block),
        context_block_chars=len(context_block),
        calendar_block_present=len(calendar_block) > 0,
    )
    prompt = get_registry().get("ask_query")
    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "question": body.question,
            "context_block": context_block,
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

    # M02 perimeter (Batch 4): citations the LLM emits must be a
    # SUBSET of the retrieved fact set. The LLM occasionally
    # hallucinates an id that resembles a tokenized UUID; that has
    # always been benign, but now the perimeter forces it to be
    # defensive too — a hallucinated id can never reference a
    # cross-record fact, because the retrieved set is already
    # record-scoped and we drop anything outside it.
    citations_in: list[dict] = list(out.get("citations", []) or [])
    citations_filtered: list[Citation] = []
    for c in citations_in:
        if not isinstance(c, dict):
            continue
        fid = c.get("fact_id")
        if isinstance(fid, str) and fid in retrieved_ids:
            citations_filtered.append(Citation(**c))

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
            # M02 perimeter: which record this thread is *about*.
            # The Conversation list page filters on this so a
            # caregiver only sees threads pinned to the active
            # record they've switched to.
            person_record_id=ctx.active_record_id,
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
            person_record_id=ctx.active_record_id,
            role="user",
            content=body.question,
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            user_id=user.id,
            person_record_id=ctx.active_record_id,
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
        citations=citations_filtered,
        retrieved_fact_count=len(facts),
        model_run_id=str(result.model_run_id),
        safety_response=out.get("safety_response"),
        error=result.error,
        conversation_id=conv_id_out,
    )
