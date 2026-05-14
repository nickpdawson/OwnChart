"""Conversations API — docs/10 first-class object.

Five endpoints carry V1:

  POST   /api/conversations                — create
  GET    /api/conversations                — list (with search)
  GET    /api/conversations/{id}           — full thread
  POST   /api/conversations/{id}/messages  — send + get cited reply
  PATCH  /api/conversations/{id}           — star / archive / rename
  DELETE /api/conversations/{id}           — soft-delete (archive)

Plus a tiny provider catalog endpoint for the Settings UI:
  GET    /api/conversations/providers      — list configured providers
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import re

from ..core.db import get_session
from ..llm import call_with_tool, get_registry
from ..llm.conversations import (
    add_user_message_and_reply,
    create_conversation,
)
from ..llm.providers import available_providers
from ..models.conversation import (
    Conversation,
    ConversationCitation,
    ConversationMessage,
)
from ..models.topic import Topic
from ..models.user import User
from .auth import get_current_user

router = APIRouter()


# ---------------------------------------------------------------------------
# Shapes


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    kind: str
    scope: dict[str, Any]
    provider: str | None
    model: str | None
    privacy_mode: str | None
    starred: bool
    archived: bool
    last_message_at: datetime | None
    created_at: datetime


class CitationOut(BaseModel):
    id: str
    citation_type: str
    subject_id: str
    claim_label: str | None
    excerpt: str | None
    note: str | None
    ordinal: int


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    provider: str | None
    model: str | None
    prompt_version: str | None
    privacy_mode: str | None
    model_run_id: str | None
    structured_output: dict[str, Any] | None
    usage: dict[str, Any] | None
    citations: list[CitationOut] = Field(default_factory=list)
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]


class CreateRequest(BaseModel):
    kind: str = "ask"
    title: str | None = None
    scope: dict[str, Any] | None = None
    first_message: str | None = None  # optional convenience


class SendRequest(BaseModel):
    content: str
    # Q-B2 (2026-05-11 PM): optional per-turn overrides. When omitted,
    # we fall through to ai.default_provider from settings.
    provider: str | None = None
    model: str | None = None


class PatchRequest(BaseModel):
    title: str | None = None
    starred: bool | None = None
    archived: bool | None = None


class ProviderShape(BaseModel):
    key: str
    label: str
    configured: bool
    capabilities: dict[str, Any]


# ---------------------------------------------------------------------------
# Conversions


def _summary(conv: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=str(conv.id),
        title=conv.title,
        kind=conv.kind,
        scope=conv.scope or {},
        provider=conv.provider,
        model=conv.model,
        privacy_mode=conv.privacy_mode,
        starred=conv.starred,
        archived=conv.archived,
        last_message_at=conv.last_message_at,
        created_at=conv.created_at,
    )


def _message_out(m: ConversationMessage, citations: list[ConversationCitation]) -> MessageOut:
    return MessageOut(
        id=str(m.id),
        role=m.role,
        content=m.content,
        provider=m.provider,
        model=m.model,
        prompt_version=m.prompt_version,
        privacy_mode=m.privacy_mode,
        model_run_id=str(m.model_run_id) if m.model_run_id else None,
        structured_output=m.structured_output,
        usage=m.usage,
        citations=[
            CitationOut(
                id=str(c.id),
                citation_type=c.citation_type,
                subject_id=str(c.subject_id),
                claim_label=c.claim_label,
                excerpt=c.excerpt,
                note=c.note,
                ordinal=c.ordinal,
            )
            for c in citations
        ],
        created_at=m.created_at,
    )


# ---------------------------------------------------------------------------
# Routes


@router.get("/providers", response_model=list[ProviderShape])
async def list_providers(_user: User = Depends(get_current_user)) -> list[ProviderShape]:
    return [ProviderShape(**p) for p in available_providers()]


# Episode-shaped question detector. When a /ask question carries a
# clinical-event word (surgery, fracture, hospitalization, …), it's
# almost always a "tell me about [event]" question that the episode
# planner is purpose-built for — it pulls same-day clinical facts,
# anesthesia, travel/life context, and aggregates HRV/sleep/RHR across
# ±21-day windows around the anchor. Routing it through general_ask
# instead misses the wearable context entirely and depends on
# substring matches between the user's natural language and the
# medically-codified labels (which often don't share words: "eye
# surgery" vs "STRABISMUS RECESSION/RESCJ 1 VER MUSC").
#
# Detection is intentionally narrow — false negatives just fall back
# to general_ask, which is fine. Caught during golden-path walk
# 2026-05-13 PM when Nick asked "I had eye surgery on may 1 2026,
# how did that affect my recovery (HR HRV) the week before and after?"
# and got "Your record doesn't show an eye surgery on May 1."
_EPISODE_KEYWORDS = re.compile(
    r"\b("
    r"surgery|surgical|operation|operative|surgeon|"
    r"procedure|operated|"
    r"fracture|broken|"
    r"hospitalization|hospitali[sz]ed|admitted|admission|"
    r"diagnos(?:ed|is)|"
    r"injury|injured|"
    r"er\s+visit|emergency\s+room"
    r")\b",
    re.IGNORECASE,
)


def _is_episode_shaped(text: str) -> bool:
    return bool(_EPISODE_KEYWORDS.search(text or ""))


async def _question_mentions_event_alias(
    db: AsyncSession, *, user_id: uuid.UUID, text: str,
) -> bool:
    """True if `text` literally contains an Event display_title or
    alias the user has registered. Used to route alias-only questions
    ("How did 2026 left eye affect my training?") into Episode
    Intelligence even when no episode-keyword fires.
    """
    from ..models.episode import Episode
    if not text:
        return False
    q = text.lower()
    rows = list((await db.execute(
        select(Episode)
        .where(Episode.user_id == user_id)
        .where((Episode.aliases != []) | (Episode.display_title.isnot(None)))  # type: ignore[arg-type]
    )).scalars().all())
    for ep in rows:
        candidates: list[str] = []
        if ep.display_title:
            candidates.append(ep.display_title)
        candidates.extend(ep.aliases or [])
        for phrase in candidates:
            p = (phrase or "").strip().lower()
            if len(p) >= 3 and p in q:
                return True
    return False


@router.post("", response_model=ConversationDetail,
             status_code=status.HTTP_201_CREATED)
async def create_conversation_route(
    body: CreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConversationDetail:
    # Auto-route episode-shaped /ask questions to Episode Intelligence.
    # Only when the caller hasn't already chosen a scope (whole_record
    # is the default for /ask). Existing dossier / source / episode-
    # scoped flows are untouched.
    scope_type = (body.scope or {}).get("type", "whole_record")
    should_route_to_ei = False
    if (
        body.first_message
        and body.kind == "ask"
        and scope_type == "whole_record"
    ):
        if _is_episode_shaped(body.first_message):
            should_route_to_ei = True
        elif await _question_mentions_event_alias(
            db, user_id=user.id, text=body.first_message,
        ):
            should_route_to_ei = True
    if should_route_to_ei:
        from ..llm.episode_intelligence import run_episode_intelligence
        ei_out = await run_episode_intelligence(
            db, user,
            natural_language=body.first_message,
            question=body.first_message,
        )
        # EI creates its own Conversation as a side effect — fetch it
        # and return in the standard shape.
        ei_conv_id = ei_out.get("conversation_id")
        if ei_conv_id:
            ei_conv = await db.get(Conversation, uuid.UUID(ei_conv_id))
            if ei_conv is not None and ei_conv.user_id == user.id:
                msgs = list((await db.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == ei_conv.id)
                    .order_by(ConversationMessage.created_at.asc())
                )).scalars().all())
                messages_out: list[MessageOut] = []
                for m in msgs:
                    cits = list((await db.execute(
                        select(ConversationCitation)
                        .where(ConversationCitation.message_id == m.id)
                        .order_by(ConversationCitation.ordinal)
                    )).scalars().all())
                    messages_out.append(_message_out(m, cits))
                return ConversationDetail(
                    **_summary(ei_conv).model_dump(),
                    messages=messages_out,
                )
        # If EI didn't produce a conversation for any reason, fall
        # through to the standard general_ask path below — degraded
        # but not broken.

    conv = await create_conversation(
        db, user,
        kind=body.kind,
        title=body.title,
        scope=body.scope,
    )
    messages_out: list[MessageOut] = []
    if body.first_message:
        u_msg, a_msg = await add_user_message_and_reply(db, user, conv, body.first_message)
        await db.refresh(conv)
        cits_u = list((await db.execute(
            select(ConversationCitation)
            .where(ConversationCitation.message_id == u_msg.id)
            .order_by(ConversationCitation.ordinal)
        )).scalars().all())
        cits_a = list((await db.execute(
            select(ConversationCitation)
            .where(ConversationCitation.message_id == a_msg.id)
            .order_by(ConversationCitation.ordinal)
        )).scalars().all())
        messages_out = [_message_out(u_msg, cits_u), _message_out(a_msg, cits_a)]

    return ConversationDetail(
        **_summary(conv).model_dump(),
        messages=messages_out,
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    starred: bool | None = Query(default=None),
    archived: bool | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[ConversationSummary]:
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.last_message_at.desc().nullslast(),
                  Conversation.created_at.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(Conversation.kind == kind)
    if starred is not None:
        stmt = stmt.where(Conversation.starred == starred)
    if archived is not None:
        stmt = stmt.where(Conversation.archived == archived)
    if q:
        # Q-B1 (2026-05-11 PM): full-text over message bodies via the
        # tsvector index added in migration 0023, OR title match.
        # plainto_tsquery is parameterized; the EXISTS subquery is
        # cheap because of the GIN index.
        pattern = f"%{q}%"
        body_match = text(
            "EXISTS (SELECT 1 FROM conversation_messages cm "
            "WHERE cm.conversation_id = conversations.id "
            "AND cm.search_tsv @@ plainto_tsquery('english', :q))"
        ).bindparams(q=q)
        stmt = stmt.where(or_(Conversation.title.ilike(pattern), body_match))
    rows = list((await db.execute(stmt)).scalars().all())
    return [_summary(c) for c in rows]


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation_route(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConversationDetail:
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    msg_rows = list((await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())
    cits_by_msg: dict[uuid.UUID, list[ConversationCitation]] = {}
    if msg_rows:
        msg_ids = [m.id for m in msg_rows]
        cit_rows = list((await db.execute(
            select(ConversationCitation)
            .where(ConversationCitation.message_id.in_(msg_ids))
            .order_by(ConversationCitation.message_id, ConversationCitation.ordinal)
        )).scalars().all())
        for c in cit_rows:
            cits_by_msg.setdefault(c.message_id, []).append(c)
    return ConversationDetail(
        **_summary(conv).model_dump(),
        messages=[_message_out(m, cits_by_msg.get(m.id, [])) for m in msg_rows],
    )


@router.post("/{conv_id}/messages", response_model=ConversationDetail)
async def post_message_route(
    conv_id: uuid.UUID,
    body: SendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConversationDetail:
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not (body.content or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="empty message")
    await add_user_message_and_reply(
        db, user, conv, body.content,
        provider_override=body.provider,
        model_override=body.model,
    )
    return await get_conversation_route(conv_id, user, db)


@router.patch("/{conv_id}", response_model=ConversationSummary)
async def patch_conversation_route(
    conv_id: uuid.UUID,
    body: PatchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConversationSummary:
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if body.title is not None:
        conv.title = body.title
    if body.starred is not None:
        conv.starred = body.starred
    if body.archived is not None:
        conv.archived = body.archived
    await db.commit()
    return _summary(conv)


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation_route(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Soft-delete = archive. Hard delete is a future admin action.
    conv.archived = True
    await db.commit()


class CandidateRefOut(BaseModel):
    id: str
    candidate_type: str
    title: str | None
    disposition: str
    # Q-A1: surface the planner's anchor match confidence so the chat
    # thread page can render a loud low-confidence banner without
    # fetching the full candidate payload.
    match_confidence: str | None = None
    match_explanation: str | None = None


@router.get("/{conv_id}/candidates", response_model=list[CandidateRefOut])
async def list_conversation_candidates(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[CandidateRefOut]:
    """Candidates produced by this conversation's most recent sensemaking
    job. Lets the chat thread page show 'Save as Episode' when an
    episode candidate is still pending."""
    from ..models.sensemaking_candidate import SensemakingCandidate
    from ..models.sensemaking_job import SensemakingJob

    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Find the most recent job whose model_run_id matches any assistant
    # message in this conversation, then list its candidates.
    msg_run_ids = [
        m.model_run_id for m in (await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv.id)
            .where(ConversationMessage.role == "assistant")
            .where(ConversationMessage.model_run_id.isnot(None))
        )).scalars().all()
        if m.model_run_id is not None
    ]
    if not msg_run_ids:
        return []
    jobs = list((await db.execute(
        select(SensemakingJob)
        .where(SensemakingJob.user_id == user.id)
        .where(SensemakingJob.model_run_id.in_(msg_run_ids))
        .order_by(SensemakingJob.created_at.desc())
    )).scalars().all())
    if not jobs:
        return []
    cands = list((await db.execute(
        select(SensemakingCandidate)
        .where(SensemakingCandidate.job_id.in_([j.id for j in jobs]))
        .order_by(SensemakingCandidate.created_at.asc())
    )).scalars().all())
    out_list: list[CandidateRefOut] = []
    for c in cands:
        planner = (c.payload or {}).get("planner") if isinstance(c.payload, dict) else None
        anchor = (planner or {}).get("anchor") if isinstance(planner, dict) else None
        out_list.append(CandidateRefOut(
            id=str(c.id),
            candidate_type=c.candidate_type,
            title=c.title,
            disposition=c.disposition,
            match_confidence=(
                anchor.get("match_confidence") if isinstance(anchor, dict) else None
            ),
            match_explanation=(
                anchor.get("match_explanation") if isinstance(anchor, dict) else None
            ),
        ))
    return out_list


# ---------------------------------------------------------------------------
# Save-as-Dossier — promote a chat to a Topic the conversation lives under.
# ---------------------------------------------------------------------------
#
# Pair of endpoints. `suggest-topic` runs an LLM to pre-fill the modal so the
# user has a starting point. `save-as-topic` takes the user-edited form and
# creates the Topic + re-scopes the conversation to it, so subsequent
# messages retrieve topic-bounded facts via the existing scope handler in
# llm/conversations.py.


class TopicSuggestion(BaseModel):
    refuse: bool = False
    refuse_reason: str | None = None
    name: str | None = None
    aliases: list[str] = []
    description: str | None = None


class SaveAsTopicBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    aliases: list[str] = []
    description: str | None = None


class SaveAsTopicResponse(BaseModel):
    topic_id: str
    slug: str
    conflict: bool = False


@router.post("/{conv_id}/suggest-topic", response_model=TopicSuggestion)
async def suggest_topic_route(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> TopicSuggestion:
    """LLM-suggested Topic for promoting this chat to a Dossier.

    Pulls the user's last question + the assistant's last answer, lists
    existing topics so the model can avoid duplicates, returns a
    `{name, aliases, description}` triple the frontend can pre-fill the
    Save-as-Dossier modal with. The user is the final editor; this is
    just a starting point.
    """
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    msgs = list((await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())
    user_msg = next((m for m in msgs if m.role == "user"), None)
    last_assistant = next(
        (m for m in reversed(msgs) if m.role == "assistant" and m.content),
        None,
    )
    if user_msg is None or last_assistant is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Conversation has no user question + assistant answer yet",
        )

    existing_topics = [
        t.name for t in (await db.execute(
            select(Topic).order_by(Topic.name)
        )).scalars().all()
    ]
    existing_block = (
        "\n".join(f"  - {n}" for n in existing_topics) if existing_topics
        else "  (none yet)"
    )

    prompt = get_registry().get("suggest_topic_from_chat")
    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "existing_topics": existing_block,
            "user_question": user_msg.content[:4000],
            "assistant_answer": last_assistant.content[:6000],
        },
        purpose="suggest_topic_from_chat",
        tool_name="emit_topic_suggestion",
    )
    if result.error and not result.tool_input:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM call failed: {result.error}",
        )
    out = result.tool_input or {}
    return TopicSuggestion(
        refuse=bool(out.get("refuse", False)),
        refuse_reason=out.get("refuse_reason"),
        name=out.get("name"),
        aliases=[a for a in (out.get("aliases") or []) if isinstance(a, str) and a.strip()],
        description=out.get("description"),
    )


@router.post("/{conv_id}/save-as-topic", response_model=SaveAsTopicResponse,
             status_code=status.HTTP_201_CREATED)
async def save_as_topic_route(
    conv_id: uuid.UUID,
    body: SaveAsTopicBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SaveAsTopicResponse:
    """Create a Topic from this conversation and re-scope the chat to it.

    After this, posting to /conversations/{conv_id}/messages retrieves
    facts via topic_membership_clause, so the thread keeps living in the
    new dossier. Frontend redirects to /dossier/<slug> on success.

    Slug collision: return existing Topic id with `conflict=true` so the
    UI can prompt "Add this conversation to your existing X dossier?"
    rather than failing the save.
    """
    from .topics import _slugify  # local import; topics.py defines it

    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    name = body.name.strip()
    slug = _slugify(name)
    aliases = [a.strip() for a in body.aliases if a and a.strip()]
    description = body.description.strip() if body.description else None

    existing = (await db.execute(
        select(Topic).where(Topic.slug == slug)
    )).scalar_one_or_none()
    if existing is not None:
        # Don't overwrite the existing topic — attach the conversation
        # to it and return conflict=true so the UI can confirm.
        conv.scope = {"type": "topic", "topic_slug": existing.slug}
        conv.kind = "dossier_followup"
        await db.commit()
        return SaveAsTopicResponse(
            topic_id=str(existing.id),
            slug=existing.slug,
            conflict=True,
        )

    topic = Topic(
        name=name,
        slug=slug,
        aliases=aliases,
        label_patterns=[],
        description=description,
        related_concepts=[],
        created_by=user.id,
    )
    db.add(topic)
    await db.flush()

    conv.scope = {"type": "topic", "topic_slug": topic.slug}
    conv.kind = "dossier_followup"  # match existing topic-conversation pattern
    await db.commit()
    await db.refresh(topic)

    return SaveAsTopicResponse(
        topic_id=str(topic.id),
        slug=topic.slug,
        conflict=False,
    )
