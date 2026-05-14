"""Episode Intelligence runtime — joins planner + LLM + persistence.

End-to-end:
  1. Resolve the anchor (fact_id / episode_id / NL phrase).
  2. Run the deterministic planner (canonical/episode_intelligence.py).
  3. Call the LLM with the planner JSON; force structured emission.
  4. Persist:
     - A SensemakingJob row (job_type='episode_intelligence').
     - A SensemakingCandidate (candidate_type='episode') holding the
       structured output — promotion to a canonical Episode is a
       separate explicit user action.
     - A Conversation (kind='episode_intelligence') with the user's
       question + the LLM's narrative + cited evidence.

Privacy: refuses cleanly when `ai.privacy_mode='off'` or PHI consent
is missing — same contract as the other sensemaking jobs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..canonical.episode_intelligence import plan_episode_intelligence
from ..core.logger import get_logger
from ..models.audit_event import AuditEvent
from ..models.conversation import (
    Conversation,
    ConversationCitation,
    ConversationMessage,
)
from ..models.sensemaking_candidate import SensemakingCandidate
from ..models.sensemaking_job import SensemakingJob
from ..models.user import User
from ..settings.registry import effective as setting_effective
from .anthropic_client import call_with_tool
from .prompts import get_registry

log = get_logger("ownchart.llm.episode_intelligence")


class EpisodeIntelligenceError(RuntimeError):
    pass


async def run_episode_intelligence(
    db: AsyncSession,
    user: User,
    *,
    fact_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
    natural_language: str | None = None,
    question: str | None = None,
    prefilled_conversation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Returns a dict with the persisted job_id / conversation_id /
    candidate_id and the rendered structured output. Caller (the
    route) wraps it in a Pydantic response.

    When `prefilled_conversation_id` is supplied, the run reuses that
    existing conversation (and the user message already on it)
    instead of creating a new one. This is the path the async
    POST /api/conversations takes: the route seeds the conversation
    synchronously, returns immediately, and schedules this function
    as a background task. The frontend polls the conversation for
    the assistant message to appear.
    """
    now = datetime.now(timezone.utc)
    privacy_mode = await setting_effective(db, user, "ai.privacy_mode")

    job = SensemakingJob(
        user_id=user.id,
        job_type="episode_intelligence",
        status="pending",
        privacy_mode=str(privacy_mode) if privacy_mode is not None else "unknown",
        scope={
            "fact_id": str(fact_id) if fact_id else None,
            "episode_id": str(episode_id) if episode_id else None,
            "natural_language": natural_language,
            "question": question,
        },
        started_at=now,
    )
    db.add(job)
    await db.flush()
    db.add(AuditEvent(
        user_id=user.id,
        event_type="episode_intelligence_started",
        subject_type="sensemaking_job",
        subject_id=str(job.id),
        detail={"privacy_mode": privacy_mode},
    ))

    if privacy_mode == "off" or not user.phi_consent_granted:
        job.status = "refused"
        job.error = "ai.privacy_mode is off or PHI consent missing"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"job_id": str(job.id), "status": "refused",
                "error": job.error, "candidate": None,
                "conversation_id": None}

    try:
        planner_payload = await plan_episode_intelligence(
            db,
            fact_id=fact_id,
            episode_id=episode_id,
            natural_language=natural_language,
            user_id=user.id,
            now=now,
        )
    except Exception as e:  # noqa: BLE001
        # Planner errors are diagnostic, not user-facing. Save the
        # detail on the job for the audit trail; tell the user
        # gracefully that we couldn't analyze yet.
        log.warning(
            "episode_intelligence_planner_failed",
            error=f"{type(e).__name__}: {e}",
        )
        job.status = "failed"
        job.error = f"planner_error: {type(e).__name__}: {e}"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"job_id": str(job.id), "status": "failed",
                "error": "OwnChart hit an error gathering the evidence for this question. Try a more specific anchor (a fact id) or check the API logs.",
                "candidate": None,
                "conversation_id": None}
    if planner_payload is None:
        # iOS asked us to make "no anchor resolved" a refused-shape
        # response so the client doesn't retry-bounce. Reasoning:
        # 'failed' implies a transient error; 'refused' is a
        # deterministic "we can't answer this with what we have."
        job.status = "refused"
        job.error = "could not resolve an anchor"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"job_id": str(job.id), "status": "refused",
                "error": (
                    "OwnChart couldn't pin this question to a specific event "
                    "on your record. Try linking it to a specific fact "
                    "(open a moment and tap 'Ask about this'), or ingest "
                    "more sources first."
                ),
                "candidate": None,
                "conversation_id": None}

    anchor = planner_payload["anchor"]

    # Conversation handling — either reuse the one the route seeded
    # (async path) or create one now (sync path).
    if prefilled_conversation_id is not None:
        conv = await db.get(Conversation, prefilled_conversation_id)
        if conv is None or conv.user_id != user.id:
            job.status = "failed"
            job.error = "prefilled_conversation_id missing or not owned by user"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return {"job_id": str(job.id), "status": "failed",
                    "error": job.error, "candidate": None,
                    "conversation_id": None}
        # Stamp the resolved anchor on the conv so the UI can navigate
        # to the Event later from this conversation.
        scope = dict(conv.scope or {})
        scope["type"] = "fact"
        scope["anchor_fact_id"] = anchor.get("fact_id")
        scope.pop("status", None)
        conv.scope = scope
        if anchor.get("label"):
            conv.title = (anchor.get("label") or conv.title)[:96]
        conv.updated_at = now
        # User message was already written by the seed step. Don't
        # duplicate.
    else:
        conv = Conversation(
            user_id=user.id,
            title=(question or anchor.get("label") or "Episode Intelligence")[:96],
            kind="episode_intelligence",
            scope={"type": "fact", "anchor_fact_id": anchor.get("fact_id")},
            created_at=now,
            updated_at=now,
        )
        db.add(conv)
        await db.flush()

        if question:
            db.add(ConversationMessage(
                conversation_id=conv.id,
                user_id=user.id,
                role="user",
                content=question,
                created_at=now,
            ))

    preferred = await setting_effective(db, user, "ai.default_provider")
    prompt = get_registry().get("episode_intelligence")
    # Trim the planner payload before sending — wearable windows can
    # be hundreds of metric points each; the LLM only needs the
    # aggregates the planner already computed.
    # Cap at 240k chars (≈ 60k tokens). The 48k cap was a relic of
    # before clinical-note extraction added 200+ same-day facts per
    # Event — at 75kB compact, the body_response section (which the
    # planner appends last) got clipped off, so the LLM hallucinated
    # "no wearable windows returned" even when they were there.
    # Caught 2026-05-14 during the Make-The-Record-Actually-Readable
    # acceptance walk.
    payload_json = json.dumps(planner_payload, default=str)[:240_000]

    # Build input_source_ids defensively — a malformed source_id in the
    # planner payload mustn't blow up the whole call.
    input_source_ids: list[uuid.UUID] = []
    for s in (planner_payload.get("what_happened", {}).get("sources") or []):
        sid = s.get("source_id") if isinstance(s, dict) else None
        if not sid:
            continue
        try:
            input_source_ids.append(uuid.UUID(str(sid)))
        except (TypeError, ValueError):
            continue

    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "anchor_fact_id": anchor.get("fact_id") or "?",
            "anchor_label": anchor.get("label") or "?",
            "anchor_date": anchor.get("date_start") or "?",
            "match_confidence": anchor.get("match_confidence") or "?",
            "match_explanation": anchor.get("match_explanation") or "?",
            "planner_json": payload_json,
            "question": question or "(no explicit question — produce the full structured answer)",
        },
        purpose="episode_intelligence",
        input_source_ids=input_source_ids,
        tool_name="emit_episode_intelligence",
        provider=preferred if isinstance(preferred, str) else None,
        # 9 required structured sections, each often multi-paragraph.
        # The 4096 default was truncating Opus mid-tool-call, leaving
        # only 2–3 keys populated and dumping XML markup into the last
        # completed parameter. Caught 2026-05-13 PM.
        max_tokens=16384,
    )

    job.model_run_id = result.model_run_id
    job.completed_at = datetime.now(timezone.utc)

    structured: dict[str, Any] | None = result.tool_input
    if result.error or not structured:
        job.status = "failed"
        job.error = result.error or "no structured tool output"
        await db.commit()
        return {"job_id": str(job.id), "status": "failed",
                "error": job.error, "candidate": None,
                "conversation_id": str(conv.id)}

    # Safety refusal short-circuit.
    if structured.get("safety_response"):
        job.status = "completed"
        db.add(SensemakingCandidate(
            user_id=user.id,
            job_id=job.id,
            candidate_type="safety_response",
            title=None,
            summary_text=structured["safety_response"],
            payload={"raw": structured, "planner": planner_payload},
            claim_label="unknown",
            disposition="pending",
        ))
        db.add(ConversationMessage(
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            content=structured["safety_response"],
            provider=result.provider,
            model=result.model,
            prompt_version=prompt.version_tag,
            privacy_mode=str(privacy_mode) if privacy_mode is not None else None,
            model_run_id=result.model_run_id,
            structured_output=structured,
            usage=result.usage,
            created_at=datetime.now(timezone.utc),
        ))
        conv.last_message_at = datetime.now(timezone.utc)
        conv.updated_at = conv.last_message_at
        await db.commit()
        return {"job_id": str(job.id), "status": "completed",
                "candidate": {"safety_response": structured["safety_response"]},
                "conversation_id": str(conv.id)}

    job.status = "completed"

    # Compose the narrative for the conversation transcript.
    # Defensive on two LLM quirks:
    #
    # 1. Section can come back as a dict ({summary, cited_fact_ids}) or
    #    a bare string. AttributeError otherwise.
    # 2. Opus occasionally leaks XML-style parameter wrappers into the
    #    value, like  <parameter name="summary">your record …</parameter>
    #    inside what should be plain prose. Strip them — the parameter
    #    name is already in the section header, the user doesn't need
    #    to see the schema scaffolding.
    import re as _re
    _XML_PARAM_OPEN = _re.compile(r"<parameter\s+name=[\"'][^\"']*[\"']\s*>", _re.IGNORECASE)
    _XML_PARAM_CLOSE = _re.compile(r"</parameter\s*>", _re.IGNORECASE)

    def _strip_xml(s: str) -> str:
        s = _XML_PARAM_OPEN.sub("", s)
        s = _XML_PARAM_CLOSE.sub("", s)
        return s.strip()

    def _section_text(key: str, dict_field: str = "summary") -> str:
        val = structured.get(key)
        if isinstance(val, dict):
            return _strip_xml(str(val.get(dict_field) or ""))
        if isinstance(val, str):
            return _strip_xml(val)
        return ""

    # Narrative shape (post-2026-05-13 directive):
    #   1. short_answer first — directly answers the user.
    #   2. Anchor / "what & where" — compact context.
    #   3. Found vs Missing — explicit, separate, no fabrication.
    #   4. Body response — endurance-aware.
    #   5. Interpretation — humble.
    #   6. Evidence — sources the answer leans on.
    # Each `_section_text` access is defensive against the dict-vs-string
    # shape Opus sometimes returns under schema confusion, and strips
    # any leaked `<parameter name="...">` markers.
    short_answer = _section_text("short_answer")
    anchor_ack = _strip_xml(structured.get("anchor_acknowledgment") or "")
    parts: list[str] = []
    if short_answer:
        parts.append(short_answer)
    if anchor_ack:
        parts.append(f"\n\n_{anchor_ack}_")
    for header, key, dict_field in [
        ("What happened",                "what_happened",         "summary"),
        ("What they did",                "what_they_did",         "translation"),
        ("Meds found",                   "meds_found",            "summary"),
        ("Meds missing",                 "meds_missing",          "summary"),
        ("Body response",                "body_response",         "summary"),
        ("Travel & life context",        "travel_and_life",       "summary"),
        ("Interpretation",               "interpretation",        "summary"),
        ("Evidence",                     "evidence_summary",      "summary"),
    ]:
        body = _section_text(key, dict_field)
        if body:
            parts.append(f"\n\n**{header}**\n{body}")
    narrative = "".join(parts).strip()

    a_msg = ConversationMessage(
        conversation_id=conv.id,
        user_id=user.id,
        role="assistant",
        content=narrative,
        provider=result.provider,
        model=result.model,
        prompt_version=prompt.version_tag,
        privacy_mode=str(privacy_mode) if privacy_mode is not None else None,
        model_run_id=result.model_run_id,
        structured_output=structured,
        usage=result.usage,
        created_at=datetime.now(timezone.utc),
    )
    db.add(a_msg)
    await db.flush()

    # Persist citations.
    for ord_, c in enumerate(structured.get("citations") or []):
        ctype = c.get("citation_type")
        sid = c.get("subject_id")
        if not ctype or not sid:
            continue
        try:
            subject_id = uuid.UUID(str(sid))
        except (TypeError, ValueError):
            continue
        db.add(ConversationCitation(
            message_id=a_msg.id,
            citation_type=ctype,
            subject_id=subject_id,
            claim_label=c.get("claim_label"),
            excerpt=c.get("excerpt"),
            note=c.get("note"),
            ordinal=ord_,
            created_at=datetime.now(timezone.utc),
        ))

    # Persist a SensemakingCandidate so the user can "Save as Episode."
    # Defend the anchor UUID parse — historically a malformed anchor
    # id 500'd the whole route.
    anchor_fact_uuids: list[uuid.UUID] = []
    anchor_fact_raw = anchor.get("fact_id")
    if isinstance(anchor_fact_raw, str):
        try:
            anchor_fact_uuids.append(uuid.UUID(anchor_fact_raw))
        except (TypeError, ValueError):
            log.warning(
                "episode_intelligence_anchor_uuid_invalid",
                anchor_fact_id=anchor_fact_raw,
            )
    candidate = SensemakingCandidate(
        user_id=user.id,
        job_id=job.id,
        candidate_type="episode",
        title=anchor.get("label") or "Episode",
        summary_text=_section_text("what_happened", "summary"),
        payload={
            "planner": planner_payload,
            "structured": structured,
            "follow_up_questions": structured.get("follow_up_questions") or [],
        },
        claim_label="source_backed",
        fact_ids=anchor_fact_uuids,
        disposition="pending",
    )
    db.add(candidate)

    conv.provider = result.provider
    conv.model = result.model
    conv.privacy_mode = str(privacy_mode) if privacy_mode is not None else None
    conv.last_message_at = a_msg.created_at
    conv.updated_at = a_msg.created_at

    db.add(AuditEvent(
        user_id=user.id,
        event_type="episode_intelligence_completed",
        subject_type="sensemaking_job",
        subject_id=str(job.id),
        detail={
            "conversation_id": str(conv.id),
            "candidate_id": str(candidate.id),
            "anchor_fact_id": anchor.get("fact_id"),
            "match_confidence": anchor.get("match_confidence"),
        },
    ))
    await db.commit()

    return {
        "job_id": str(job.id),
        "status": "completed",
        "conversation_id": str(conv.id),
        "candidate": {
            "id": str(candidate.id),
            "structured": structured,
            "planner": planner_payload,
        },
    }


# ---------------------------------------------------------------------------
# Async entry point — used by POST /api/conversations to seed an EI
# conversation immediately and schedule the planner+LLM in the
# background. Avoids 60+ second blocking HTTP requests.


async def seed_episode_intelligence_conversation(
    db: AsyncSession,
    user: User,
    *,
    first_message: str,
) -> Conversation:
    """Create a kind=episode_intelligence Conversation + its user
    message synchronously. Returns the persisted Conversation (with
    its row id) so the route can include it in the response body
    immediately, before the planner / LLM run.

    The conversation's scope is marked `status: "running"` so the
    frontend knows to show a "OwnChart is reading the record"
    placeholder and poll until the assistant message appears.
    """
    now = datetime.now(timezone.utc)
    conv = Conversation(
        user_id=user.id,
        title=(first_message or "Episode Intelligence")[:96],
        kind="episode_intelligence",
        scope={"type": "whole_record", "status": "running"},
        created_at=now,
        updated_at=now,
    )
    db.add(conv)
    await db.flush()
    db.add(ConversationMessage(
        conversation_id=conv.id,
        user_id=user.id,
        role="user",
        content=first_message,
        created_at=now,
    ))
    conv.last_message_at = now
    await db.commit()
    return conv


async def run_episode_intelligence_in_background(
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    natural_language: str,
) -> None:
    """Background task entry point. Opens its own SessionLocal —
    callers (FastAPI BackgroundTasks) run this after the response
    has already been sent and the request's DB session is closed.

    On any exception, persists an assistant message with the error
    so the frontend's poll loop terminates and the user sees a
    clear failure instead of a forever-spinner.
    """
    from ..core.db import SessionLocal  # local import: avoid cycles
    from ..models.user import User as _User

    async with SessionLocal() as db:
        user = await db.get(_User, user_id)
        if user is None:
            log.warning("ei_background_user_missing", user_id=str(user_id))
            return
        try:
            await run_episode_intelligence(
                db, user,
                natural_language=natural_language,
                question=natural_language,
                prefilled_conversation_id=conversation_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ei_background_failed",
                conversation_id=str(conversation_id),
                error=f"{type(e).__name__}: {e}",
            )
            # Surface the failure to the user instead of hanging.
            try:
                conv = await db.get(Conversation, conversation_id)
                if conv is not None and conv.user_id == user_id:
                    scope = dict(conv.scope or {})
                    scope["status"] = "failed"
                    conv.scope = scope
                    db.add(ConversationMessage(
                        conversation_id=conv.id,
                        user_id=user_id,
                        role="assistant",
                        content=(
                            "OwnChart hit an error while reading your "
                            "record for this question. The system has "
                            "logged the detail; try a more specific "
                            "anchor, or check back in a moment."
                        ),
                        created_at=datetime.now(timezone.utc),
                    ))
                    conv.last_message_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:  # noqa: BLE001
                pass
