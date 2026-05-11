"""Conversation service — the runtime for docs/10 saved AI threads.

Owns:
  - Creating Conversations with a scope.
  - Adding a user message, scoping retrieval, calling the LLM,
    persisting the assistant reply + citations.
  - Reusing the existing prompt registry + provider abstraction.

Specialized job types (Episode Intelligence, Make Sense, etc.) wrap
their own prompts but share this Conversation persistence layer.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.audit_event import AuditEvent
from ..models.conversation import (
    Conversation,
    ConversationCitation,
    ConversationMessage,
)
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.topic import Topic
from ..models.user import User
from ..retrieval.topics import search_facts
from ..settings.registry import effective as setting_effective
from .anthropic_client import call_with_tool
from .prompts import get_registry

log = get_logger("ownchart.llm.conversations")


# ---------------------------------------------------------------------------
# Creation


async def create_conversation(
    db: AsyncSession,
    user: User,
    *,
    kind: str = "ask",
    title: str | None = None,
    scope: dict[str, Any] | None = None,
) -> Conversation:
    now = datetime.now(timezone.utc)
    conv = Conversation(
        user_id=user.id,
        title=title,
        kind=kind,
        scope=scope or {"type": "whole_record"},
        starred=False,
        archived=False,
        last_message_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(conv)
    await db.flush()
    db.add(AuditEvent(
        user_id=user.id,
        event_type="conversation_created",
        subject_type="conversation",
        subject_id=str(conv.id),
        detail={"kind": kind, "scope": scope or {"type": "whole_record"}},
    ))
    await db.commit()
    return conv


# ---------------------------------------------------------------------------
# Retrieval — gather evidence for a scoped question


async def _gather_evidence(
    db: AsyncSession,
    *,
    scope: dict[str, Any],
    question: str,
    limit: int = 25,
) -> tuple[list[ExtractedFact], list[SourceDocument]]:
    """Pull facts + sources relevant to the conversation's scope + the
    user's latest question.

    V1 strategy:
      - For `period` scope, filter facts whose date_start falls in the
        window; also run search_facts() over the question and intersect.
      - For `source` scope, fetch all facts anchored to that source.
      - For `topic` scope, use facts_for_topic().
      - For `episode` scope, fetch all member facts.
      - For `whole_record` (default), run search_facts() against the
        question.
    """
    kind = scope.get("type") or "whole_record"
    facts: list[ExtractedFact] = []
    sources: list[SourceDocument] = []

    if kind == "whole_record":
        facts = await search_facts(db, question, limit=limit)

    elif kind == "period":
        from datetime import datetime as _dt
        start = scope.get("period", {}).get("from")
        end = scope.get("period", {}).get("to")
        start_dt = _dt.fromisoformat(start) if start else None
        end_dt = _dt.fromisoformat(end) if end else None
        stmt = (
            select(ExtractedFact)
            .where(ExtractedFact.review_state.notin_(
                ("deferred", "rejected", "source_only")
            ))
            .order_by(ExtractedFact.date_start.desc().nullslast())
            .limit(limit * 2)
        )
        if start_dt is not None:
            stmt = stmt.where(ExtractedFact.date_start >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(ExtractedFact.date_start <= end_dt)
        rows = list((await db.execute(stmt)).scalars().all())
        # Intersect with question-search to keep relevance.
        searched = await search_facts(db, question, limit=limit)
        searched_ids = {s.id for s in searched}
        facts = [r for r in rows if r.id in searched_ids] or rows[:limit]

    elif kind == "source":
        sids = [uuid.UUID(s) for s in scope.get("source_ids", [])]
        from ..models.evidence_anchor import EvidenceAnchor
        anchor_ids = list((await db.execute(
            select(EvidenceAnchor.id)
            .where(EvidenceAnchor.source_document_id.in_(sids))
        )).scalars().all())
        if anchor_ids:
            stmt = (
                select(ExtractedFact)
                .where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
                .order_by(ExtractedFact.date_start.desc().nullslast())
                .limit(limit)
            )
            facts = list((await db.execute(stmt)).scalars().all())

    elif kind == "topic":
        slug = scope.get("topic_slug")
        if slug:
            topic = (await db.execute(
                select(Topic).where(Topic.slug == slug)
            )).scalar_one_or_none()
            if topic is not None:
                from ..retrieval.topics import facts_for_topic
                facts = await facts_for_topic(db, topic, limit=limit)

    elif kind == "episode":
        from ..models.episode import EpisodeMember
        eid = scope.get("episode_id")
        if eid:
            episode_id = uuid.UUID(eid)
            fact_ids = list((await db.execute(
                select(EpisodeMember.subject_id)
                .where(EpisodeMember.episode_id == episode_id)
                .where(EpisodeMember.member_type == "fact")
            )).scalars().all())
            if fact_ids:
                stmt = select(ExtractedFact).where(ExtractedFact.id.in_(fact_ids))
                facts = list((await db.execute(stmt)).scalars().all())

    elif kind == "fact":
        # 2026-05-11 PM bug fix: a fact-scoped conversation needs the
        # *surrounding* evidence, not just the one fact. Run the
        # episode-intelligence planner to gather same-day clinical +
        # anesthesia + perioperative_support + travel/life around
        # the anchor, then pass that set into the LLM context.
        anchor_id_str = scope.get("anchor_fact_id")
        if anchor_id_str:
            from ..canonical.episode_intelligence import (
                plan_episode_intelligence,
            )
            try:
                anchor_uuid = uuid.UUID(str(anchor_id_str))
            except (TypeError, ValueError):
                anchor_uuid = None
            if anchor_uuid is not None:
                planner = await plan_episode_intelligence(
                    db, fact_id=anchor_uuid,
                )
                if planner is not None:
                    fact_id_strs: list[str] = []
                    for bucket in ("procedures", "conditions", "encounters"):
                        for f in planner.get("what_happened", {}).get(bucket, []) or []:
                            if isinstance(f, dict) and f.get("fact_id"):
                                fact_id_strs.append(str(f["fact_id"]))
                    for f in planner.get("anesthesia_meds", {}).get("facts", []) or []:
                        if isinstance(f, dict) and f.get("fact_id"):
                            fact_id_strs.append(str(f["fact_id"]))
                    for f in (planner.get("perioperative_support_meds", {})
                              .get("facts", []) or []):
                        if isinstance(f, dict) and f.get("fact_id"):
                            fact_id_strs.append(str(f["fact_id"]))
                    for f in planner.get("travel_and_life", {}).get("events", []) or []:
                        if isinstance(f, dict) and f.get("fact_id"):
                            fact_id_strs.append(str(f["fact_id"]))
                    # Anchor itself.
                    fact_id_strs.append(str(anchor_uuid))
                    fact_uuids: list[uuid.UUID] = []
                    for s in fact_id_strs:
                        try:
                            fact_uuids.append(uuid.UUID(s))
                        except ValueError:
                            continue
                    if fact_uuids:
                        rows = list((await db.execute(
                            select(ExtractedFact)
                            .where(ExtractedFact.id.in_(fact_uuids))
                        )).scalars().all())
                        facts = rows
                    # Carry wearable-window aggregates into the prompt
                    # via a synthetic system fact. Cheap; lets the LLM
                    # reason about HRV/sleep deltas without re-querying.
                    body = planner.get("body_response", {}).get("windows", [])
                    if body:
                        summary_bits: list[str] = []
                        for w in body:
                            m = w.get("metrics", {})
                            if not m:
                                continue
                            metric_summary = ", ".join(
                                f"{k}={v.get('mean')}{v.get('unit') or ''} (n={v.get('n')})"
                                for k, v in m.items()
                            )
                            summary_bits.append(f"{w['name']}: {metric_summary}")
                        if summary_bits:
                            from ..models.extracted_fact import ExtractedFact as _EF
                            ghost = _EF(
                                id=uuid.uuid4(),  # not persisted; in-memory only
                                fact_type="observation",
                                label="Wearable summary across recovery windows",
                                description="; ".join(summary_bits),
                                date_start=None,
                                evidence_anchor_ids=[],
                                extraction_method="planner_summary",
                                review_state="confirmed",
                                significance="background",
                            )
                            facts.append(ghost)

    # Resolve sources for any anchors we found.
    if facts:
        from ..models.evidence_anchor import EvidenceAnchor
        anchor_ids: list[uuid.UUID] = []
        for f in facts:
            if f.evidence_anchor_ids:
                anchor_ids.append(f.evidence_anchor_ids[0])
        if anchor_ids:
            sid_rows = (await db.execute(
                select(EvidenceAnchor.source_document_id)
                .where(EvidenceAnchor.id.in_(anchor_ids))
                .distinct()
            )).scalars().all()
            sid_list = [s for s in sid_rows if s is not None]
            if sid_list:
                src_rows = (await db.execute(
                    select(SourceDocument).where(SourceDocument.id.in_(sid_list))
                )).scalars().all()
                sources = list(src_rows)

    return facts[:limit], sources


def _scope_description(scope: dict[str, Any]) -> str:
    kind = scope.get("type") or "whole_record"
    if kind == "whole_record":
        return "the entire record"
    if kind == "period":
        p = scope.get("period", {})
        return f"records between {p.get('from', '?')} and {p.get('to', '?')}"
    if kind == "source":
        ids = scope.get("source_ids", [])
        return f"sources {ids[0]}…" if ids else "no specific source"
    if kind == "topic":
        return f"dossier '{scope.get('topic_slug')}'"
    if kind == "episode":
        return f"episode {scope.get('episode_id')}"
    if kind == "fact":
        return f"fact {scope.get('anchor_fact_id')}"
    return kind


def _evidence_block(facts: list[ExtractedFact]) -> str:
    if not facts:
        return "(none retrieved)"
    lines: list[str] = []
    for f in facts:
        date = f.date_start.date().isoformat() if f.date_start else "?"
        label = f.display_label or f.label
        sig = f.significance or "background"
        lines.append(
            f"- fact_id={f.id} type={f.fact_type} date={date} sig={sig}\n"
            f"  label: {label}"
            + (f"\n  desc: {f.description[:200]}" if f.description else "")
        )
    return "\n".join(lines)


def _history_block(history: list[ConversationMessage]) -> str:
    if not history:
        return "(none)"
    lines: list[str] = []
    for m in history[-6:]:  # last 6 turns for prompt economy
        role = m.role
        body = (m.content or "").strip()
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append(f"{role}: {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sending


_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


async def add_user_message_and_reply(
    db: AsyncSession,
    user: User,
    conv: Conversation,
    content: str,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> tuple[ConversationMessage, ConversationMessage]:
    """Append a user message to a conversation and synthesize the
    assistant reply. Returns the two messages.

    Refuses gracefully when `ai.privacy_mode` is off or PHI consent
    is missing: a system-style assistant message explains why.

    Q-B2 (2026-05-11 PM): caller can pass provider_override /
    model_override per turn. Without overrides we read
    ai.default_provider from settings.
    """
    now = datetime.now(timezone.utc)
    user_msg = ConversationMessage(
        conversation_id=conv.id,
        user_id=user.id,
        role="user",
        content=content,
        created_at=now,
    )
    db.add(user_msg)
    await db.flush()

    privacy_mode = await setting_effective(db, user, "ai.privacy_mode")
    if privacy_mode == "off" or not user.phi_consent_granted:
        refusal = (
            "OwnChart's LLM access is off "
            "(ai.privacy_mode = 'off' or PHI consent not granted). "
            "Open Settings → Privacy and AI to enable, then try again."
        )
        sys_msg = ConversationMessage(
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            content=refusal,
            privacy_mode=privacy_mode,
            created_at=datetime.now(timezone.utc),
        )
        db.add(sys_msg)
        conv.last_message_at = sys_msg.created_at
        conv.updated_at = sys_msg.created_at
        await db.commit()
        return user_msg, sys_msg

    facts, sources = await _gather_evidence(
        db, scope=conv.scope or {"type": "whole_record"}, question=content,
    )

    # Load recent history (this conversation only) for prompt context.
    history = list((await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .where(ConversationMessage.id != user_msg.id)
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())

    # Resolve provider preference:
    #   1. per-turn override (Q-B2)
    #   2. per-conversation override stored on conv.scope["default_provider"]
    #   3. ai.default_provider user setting
    preferred = provider_override
    if not preferred and isinstance(conv.scope, dict):
        sc = conv.scope.get("default_provider")
        if isinstance(sc, str):
            preferred = sc
    if not preferred:
        s = await setting_effective(db, user, "ai.default_provider")
        if isinstance(s, str) and s not in ("auto", ""):
            preferred = s
    prompt = get_registry().get("general_ask")
    result = await call_with_tool(
        db,
        user,
        prompt,
        user_vars={
            "question": content,
            "scope_description": _scope_description(conv.scope or {}),
            "evidence_count": str(len(facts)),
            "evidence_block": _evidence_block(facts),
            "history_block": _history_block(history),
            "needs_title": "yes" if not conv.title else "no",
        },
        purpose="general_ask",
        input_source_ids=[s.id for s in sources],
        tool_name="emit_answer",
        provider=preferred,
    )

    answer_text: str
    structured: dict[str, Any] | None = None
    citations_in: list[dict[str, Any]] = []
    follow_ups: list[str] = []
    if result.tool_input:
        structured = result.tool_input
        if structured.get("safety_response"):
            answer_text = structured["safety_response"]
        else:
            answer_text = structured.get("answer") or ""
        citations_in = structured.get("citations") or []
        follow_ups = structured.get("follow_up_questions") or []
    else:
        answer_text = result.raw_text or "(no answer produced)"

    assistant_msg = ConversationMessage(
        conversation_id=conv.id,
        user_id=user.id,
        role="assistant",
        content=answer_text,
        provider=result.provider,
        model=result.model,
        prompt_version=prompt.version_tag,
        privacy_mode=privacy_mode if isinstance(privacy_mode, str) else None,
        model_run_id=result.model_run_id,
        structured_output={
            **(structured or {}),
            "follow_up_questions": follow_ups,
        },
        usage=result.usage,
        created_at=datetime.now(timezone.utc),
    )
    db.add(assistant_msg)
    await db.flush()

    # Persist citations as their own rows, deduped on (type, subject_id).
    seen: set[tuple[str, uuid.UUID]] = set()
    for ord_, c in enumerate(citations_in):
        ctype = c.get("citation_type")
        sid = c.get("subject_id")
        if not ctype or not sid:
            continue
        try:
            subject_id = uuid.UUID(str(sid))
        except (TypeError, ValueError):
            continue
        if (ctype, subject_id) in seen:
            continue
        seen.add((ctype, subject_id))
        db.add(ConversationCitation(
            message_id=assistant_msg.id,
            citation_type=ctype,
            subject_id=subject_id,
            claim_label=c.get("claim_label"),
            excerpt=c.get("excerpt"),
            note=c.get("note"),
            ordinal=ord_,
            created_at=datetime.now(timezone.utc),
        ))

    # Denormalize last provider/model on the conversation header.
    conv.provider = result.provider
    conv.model = result.model
    conv.privacy_mode = privacy_mode if isinstance(privacy_mode, str) else None
    conv.last_message_at = assistant_msg.created_at
    conv.updated_at = assistant_msg.created_at
    # Q-B3 (2026-05-11 PM): prefer the LLM's six-word short_title when
    # the assistant emitted one on this turn. Falls back to the first
    # user message (truncated) so the list view always shows something
    # meaningful.
    if not conv.title:
        short = (structured or {}).get("short_title") if structured else None
        if isinstance(short, str) and short.strip():
            # Cap at 64 chars defensively even though the prompt asks
            # for ≤6 words.
            conv.title = short.strip()[:64].rstrip(".!?")
        else:
            conv.title = content[:96] + ("…" if len(content) > 96 else "")

    await db.commit()
    return user_msg, assistant_msg
