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
) -> dict[str, Any]:
    """Returns a dict with the persisted job_id / conversation_id /
    candidate_id and the rendered structured output. Caller (the
    route) wraps it in a Pydantic response."""
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

    # Create the Conversation up front so the user message gets a home
    # before the LLM call.
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
    payload_json = json.dumps(planner_payload, default=str)[:48_000]

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
    # Defensive: the LLM can emit a section field either as the
    # documented dict shape ({summary, cited_fact_ids}) or as a bare
    # string. Caught when Nick's "eye surgery on may 1 2026" question
    # crashed with AttributeError on str.get. Handle both.
    def _section_text(key: str, dict_field: str = "summary") -> str:
        val = structured.get(key)
        if isinstance(val, dict):
            return str(val.get(dict_field) or "")
        if isinstance(val, str):
            return val
        return ""

    sections = [
        structured.get("anchor_acknowledgment") or "",
        f"\n\n**What happened**\n{_section_text('what_happened', 'summary')}",
        f"\n\n**What they did**\n{_section_text('what_they_did', 'translation')}",
        f"\n\n**Anesthesia & intraoperative meds**\n{_section_text('anesthesia', 'summary')}",
        f"\n\n**Travel & life context**\n{_section_text('travel_and_life', 'summary')}",
        f"\n\n**Body response**\n{_section_text('body_response', 'summary')}",
        f"\n\n**Interpretation**\n{_section_text('interpretation')}",
    ]
    narrative = "".join(sections).strip()

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
