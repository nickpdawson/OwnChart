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
from ..retrieval.calendar_life_context import (
    fetch_calendar_life_context,
    format_calendar_context_block,
)
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
    person_record_id: uuid.UUID | None = None,
) -> Conversation:
    """Create a Conversation row.

    M02 perimeter (Batch 5): `person_record_id` is the record this
    thread is *about*; required for all route-driven calls. Defaults
    to None for legacy in-process callers (workers, tests) but the
    route layer always passes ctx.active_record_id.
    """
    now = datetime.now(timezone.utc)
    conv = Conversation(
        user_id=user.id,
        person_record_id=person_record_id,
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
        person_record_id=person_record_id,
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
    user_id: uuid.UUID | None = None,
    person_record_id: uuid.UUID | None = None,
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

    M02 perimeter (Batch 5): when `person_record_id` is supplied,
    EVERY scope branch's retrieval is constrained to that record.
    This is load-bearing because conversations feed LLM context;
    without it, a caregiver chatting about a parent's topic could
    surface their own facts that happen to share a slug.
    """
    kind = scope.get("type") or "whole_record"
    facts: list[ExtractedFact] = []
    sources: list[SourceDocument] = []

    def _scope_record(stmt):
        """Apply the active-record filter when caller scoped it."""
        if person_record_id is None:
            return stmt
        return stmt.where(ExtractedFact.person_record_id == person_record_id)

    if kind == "whole_record":
        facts = await search_facts(
            db, question, limit=limit,
            user_id=user_id, person_record_id=person_record_id,
        )

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
        stmt = _scope_record(stmt)
        if start_dt is not None:
            stmt = stmt.where(ExtractedFact.date_start >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(ExtractedFact.date_start <= end_dt)
        rows = list((await db.execute(stmt)).scalars().all())
        # Intersect with question-search to keep relevance.
        searched = await search_facts(
            db, question, limit=limit,
            user_id=user_id, person_record_id=person_record_id,
        )
        searched_ids = {s.id for s in searched}
        facts = [r for r in rows if r.id in searched_ids] or rows[:limit]

    elif kind == "source":
        sids = [uuid.UUID(s) for s in scope.get("source_ids", [])]
        from ..models.evidence_anchor import EvidenceAnchor
        from ..models.source_document import SourceDocument as _SD
        # M02 perimeter: only resolve anchors for sources that live
        # under the active record. A user passing scope.source_ids
        # that point at another record cannot retrieve via this path.
        source_filter = _SD.id.in_(sids)
        if person_record_id is not None:
            source_filter = source_filter & (_SD.person_record_id == person_record_id)
        in_scope_source_ids = list((await db.execute(
            select(_SD.id).where(source_filter)
        )).scalars().all())
        if in_scope_source_ids:
            anchor_ids = list((await db.execute(
                select(EvidenceAnchor.id)
                .where(EvidenceAnchor.source_document_id.in_(in_scope_source_ids))
            )).scalars().all())
            if anchor_ids:
                stmt = (
                    select(ExtractedFact)
                    .where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
                    .order_by(ExtractedFact.date_start.desc().nullslast())
                    .limit(limit)
                )
                stmt = _scope_record(stmt)
                facts = list((await db.execute(stmt)).scalars().all())

    elif kind == "topic":
        slug = scope.get("topic_slug")
        if slug:
            topic_stmt = select(Topic).where(Topic.slug == slug)
            if person_record_id is not None:
                topic_stmt = topic_stmt.where(
                    Topic.person_record_id == person_record_id,
                )
            topic = (await db.execute(topic_stmt)).scalar_one_or_none()
            if topic is not None:
                from ..retrieval.topics import facts_for_topic
                # Topic-scoped chats also need to honor the user's
                # current question. If the dossier is "Left Knee" but
                # the user asks "look at my OrthoVirginia records,"
                # retrieval must reach OrthoVirginia — not just
                # left-knee-aliased facts. Round-3 read 2026-05-15 PM.
                topic_facts = await facts_for_topic(
                    db, topic, limit=limit,
                    person_record_id=person_record_id,
                )
                question_facts = await search_facts(
                    db, question, limit=limit,
                    user_id=user_id, person_record_id=person_record_id,
                )
                # Topic facts first (the dossier context the user
                # explicitly opened), then question-driven results.
                seen_ids: set = set()
                merged: list[ExtractedFact] = []
                for f in topic_facts + question_facts:
                    if f.id in seen_ids:
                        continue
                    seen_ids.add(f.id)
                    merged.append(f)
                    if len(merged) >= limit:
                        break
                facts = merged

    elif kind == "episode":
        from ..models.episode import Episode, EpisodeMember
        eid = scope.get("episode_id")
        if eid:
            episode_id = uuid.UUID(eid)
            # M02 perimeter: only resolve the episode if it lives on
            # the active record. Episodes carry person_record_id from
            # migration 0029.
            ep_stmt = select(Episode.id).where(Episode.id == episode_id)
            if person_record_id is not None:
                ep_stmt = ep_stmt.where(
                    Episode.person_record_id == person_record_id,
                )
            in_scope = (await db.execute(ep_stmt)).scalar_one_or_none()
            if in_scope is not None:
                fact_ids = list((await db.execute(
                    select(EpisodeMember.subject_id)
                    .where(EpisodeMember.episode_id == episode_id)
                    .where(EpisodeMember.member_type == "fact")
                )).scalars().all())
                if fact_ids:
                    stmt = select(ExtractedFact).where(
                        ExtractedFact.id.in_(fact_ids),
                    )
                    stmt = _scope_record(stmt)
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
            # M02 perimeter: don't run the planner against an anchor
            # fact that lives on another record. Resolve + scope-check
            # the anchor first; a fact-scoped chat against a sibling
            # record's anchor must produce zero context.
            if anchor_uuid is not None and person_record_id is not None:
                anchor_check = (await db.execute(
                    select(ExtractedFact.id)
                    .where(ExtractedFact.id == anchor_uuid)
                    .where(ExtractedFact.person_record_id == person_record_id)
                )).scalar_one_or_none()
                if anchor_check is None:
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
                        ids_stmt = (
                            select(ExtractedFact)
                            .where(ExtractedFact.id.in_(fact_uuids))
                        )
                        ids_stmt = _scope_record(ids_stmt)
                        rows = list((await db.execute(ids_stmt)).scalars().all())
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
                            metric_parts: list[str] = []
                            for k, v in m.items():
                                # Planner mixes shapes: mean-based dicts
                                # ({mean, unit, n}), sum-based dicts
                                # ({total, daily_mean, active_days}), and
                                # scalar ints/floats (training_gap_days).
                                # Caught 2026-05-15 PM: assuming dict-only
                                # crashed chat send with AttributeError.
                                if isinstance(v, dict):
                                    if "mean" in v:
                                        metric_parts.append(
                                            f"{k}={v.get('mean')}{v.get('unit') or ''} (n={v.get('n')})"
                                        )
                                    elif "total" in v:
                                        unit = v.get("unit") or ""
                                        bits = [f"total={v.get('total')}{unit}"]
                                        if v.get("daily_mean") is not None:
                                            bits.append(f"daily_mean={v.get('daily_mean')}{unit}")
                                        if v.get("active_days") is not None:
                                            bits.append(f"active_days={v.get('active_days')}")
                                        metric_parts.append(f"{k}={{ {', '.join(bits)} }}")
                                    else:
                                        metric_parts.append(f"{k}={v}")
                                else:
                                    metric_parts.append(f"{k}={v}")
                            metric_summary = ", ".join(metric_parts)
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
                # M02 perimeter: defense-in-depth on the source
                # resolution. The fact set is already record-scoped
                # above, but a stray anchor pointing at a cross-record
                # source from a pre-migration row should still be
                # caught.
                src_stmt = select(SourceDocument).where(
                    SourceDocument.id.in_(sid_list),
                )
                if person_record_id is not None:
                    src_stmt = src_stmt.where(
                        SourceDocument.person_record_id == person_record_id,
                    )
                src_rows = (await db.execute(src_stmt)).scalars().all()
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


# Source Authority Doctrine classifier (2026-05-15 PM, Nick).
#
# Six-tier authority hierarchy — see user-docs/SOURCE_AUTHORITY_DOCTRINE.md
# for the full doctrine. The classifier maps `original_filename`,
# `source_type`, and `source_label` to a tier label that downstream
# code uses for retrieval diversity + prompt ranking + evidence
# display.
#
# Tiers, highest first (must match user-docs/SOURCE_AUTHORITY_DOCTRINE.md):
#   1 primary_event           — op note, pathology, imaging, lab, device
#   2 specialist_proximate    — specialty notes about the event topic
#   3 contemporaneous_support — PT, discharge, prescriptions, visit summaries
#   4 ehr_summary             — patient summaries, problem lists, CCD
#   5 self_reported_history   — pre-op H&P / surgical history copy-forward
#   6 model_inference         — LLM-derived (only stamped at fact level)
#
# `unknown` is the catch-all when no rule fires. The Ask prompt
# treats `unknown` as tier 4 for ranking purposes.

# Filename keywords that map to primary_event records.
_PRIMARY_EVENT_KEYWORDS: tuple[str, ...] = (
    "operative report", "operative note", "op note", "surgical report",
    "pathology", "histopathology", "imaging study", "imaging",
    "radiology", "lab report", "laboratory", "ekg report", "ecg report",
)

# Filename keywords that map to self-reported history (lowest non-inference).
# These are pre-op H&Ps and surgical history copy-forwards — patient
# said it during intake, not the record of the event itself.
_SELF_REPORTED_KEYWORDS: tuple[str, ...] = (
    "anesthesia preprocedure", "preprocedure evaluation",
    "pre-op evaluation", "pre op evaluation", "preop evaluation",
    "surgical history", "past surgical history", "psh", "intake",
)

# Filename keywords that map to contemporaneous support.
_CONTEMPORANEOUS_KEYWORDS: tuple[str, ...] = (
    "discharge instructions", "discharge summary",
    "physical therapy", "pt evaluation", "pt note",
    "prescription", "medication list",
    "visit summary", "encounter summary", "office visit",
    "consult", "progress note",
)

# Filename keywords that map to ehr_summary.
_EHR_SUMMARY_KEYWORDS: tuple[str, ...] = (
    "patient summary", "continuity of care", "ccda", "ccd ",
    "problem list", "health summary",
)

# Specialty practice hints that promote a contemporaneous-support
# encounter to `specialist_proximate` when the source_label names a
# specialty practice (orthopedics for knees, ophthalmology for eyes,
# etc.). Per the doctrine, copied clinical history from a non-specialty
# encounter must NOT outrank records from the specialty/source closest
# to the event.
_SPECIALTY_LABEL_HINTS: tuple[str, ...] = (
    "orthovirginia", "ortho", "stanford orthopedics", "kaiser ortho",
    "bridger ortho", "cardiology", "dermatology", "endocrinology",
    "gastroenterology", "neurology", "ophthalmology", "ent",
    "pulmonology", "rheumatology", "urology", "nephrology",
    "audiology", "hearing center", "audiometric",
)

# Source-type bucket → default tier. Used when filename keywords don't
# fire (e.g. a healthkit_sync source has no filename), or as a floor.
_SOURCE_TYPE_FLOORS: dict[str, str] = {
    "health_auto_export": "primary_event",   # device-recorded data
    "native_healthkit":   "primary_event",   # device-recorded data
    "fhir_bundle":        "ehr_summary",     # bundle-level default
    "ccda_xml":           "ehr_summary",     # CCDA is a summary unless filename narrows it
    "clinical_note":      "specialist_proximate",  # default; refined below
}


def _source_quality_tier(
    source_label: str | None,
    filename: str | None,
    source_type: str | None = None,
) -> str:
    """Return the authority tier for a source document.

    Doctrine-aligned, deterministic. Order of checks:
      1. Filename keyword scan against each tier's vocabulary (most
         diagnostic — "Anesthesia Preprocedure Evaluation" is always
         self_reported regardless of which practice emitted it).
      2. Source-type floor (HealthKit / Auto Export → primary_event).
      3. Specialty-label promotion for tier-3 records that came from a
         specialty practice (e.g. an OrthoVirginia visit summary
         doesn't sit in `contemporaneous_support`; it's
         `specialist_proximate` for any knee/ortho question).
    """
    s = (source_label or "").lower()
    f = (filename or "").lower()
    t = (source_type or "").lower()

    # 1. Filename keyword scan — strongest signal.
    if any(k in f for k in _PRIMARY_EVENT_KEYWORDS):
        return "primary_event"
    if any(k in f for k in _SELF_REPORTED_KEYWORDS):
        return "self_reported_history"
    has_specialty = any(h in s for h in _SPECIALTY_LABEL_HINTS)
    if any(k in f for k in _CONTEMPORANEOUS_KEYWORDS):
        return "specialist_proximate" if has_specialty else "contemporaneous_support"
    if any(k in f for k in _EHR_SUMMARY_KEYWORDS):
        return "specialist_proximate" if has_specialty else "ehr_summary"

    # 2. Source-type floor (device-emitted data is always primary).
    if t in _SOURCE_TYPE_FLOORS:
        floor = _SOURCE_TYPE_FLOORS[t]
        if floor == "specialist_proximate" and not has_specialty:
            return "contemporaneous_support"
        # FHIR / CCDA bundles from a specialty practice are
        # specialty-primary records (the practice's own emit), not
        # generic summaries. Promote `ehr_summary` floor to
        # `specialist_proximate` when has_specialty fires.
        if floor == "ehr_summary" and has_specialty:
            return "specialist_proximate"
        return floor

    # 3. Specialty-label-only promotion as the last resort.
    if has_specialty:
        return "specialist_proximate"
    return "unknown"


# Ranking key for retrieval ordering. Lower = higher authority.
_TIER_RANK: dict[str, int] = {
    "primary_event":           1,
    "specialist_proximate":    2,
    "contemporaneous_support": 3,
    "ehr_summary":             4,
    "self_reported_history":   5,
    "model_inference":         6,
    "unknown":                 4,  # treat unknown as ehr_summary for ranking
}


def _evidence_block(
    facts: list[ExtractedFact],
    fact_source_meta: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> str:
    """Render the retrieved-evidence block for the Ask LLM.

    `fact_source_meta` maps fact_id → {source_label, original_filename,
    source_quality}. When supplied, each line carries provenance so
    the LLM can rank citations (P0-3 from 2026-05-15 PM read: ACL
    answer was citing Stanford pre-op surgical history instead of the
    OrthoVirginia primary record).
    """
    if not facts:
        return "(none retrieved)"
    lines: list[str] = []
    for f in facts:
        date = f.date_start.date().isoformat() if f.date_start else "?"
        label = f.display_label or f.label
        sig = f.significance or "background"
        meta = (fact_source_meta or {}).get(f.id) or {}
        src_label = meta.get("source_label") or ""
        src_file = meta.get("original_filename") or ""
        quality = meta.get("source_quality") or "unknown"
        head = (
            f"- fact_id={f.id} type={f.fact_type} date={date} sig={sig}"
            f" quality={quality}"
        )
        body = f"  label: {label}"
        if src_label or src_file:
            body += f"\n  source: {src_label or '?'} · {src_file or '?'}"
        if f.description:
            body += f"\n  desc: {f.description[:200]}"
        lines.append(head + "\n" + body)
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
    # M02 perimeter (Batch 5): inherit the record scope from the
    # parent Conversation. Every message + assistant reply on this
    # thread carries the same person_record_id; retrieval below uses
    # the same value to constrain evidence gathering.
    conv_record_id = conv.person_record_id
    user_msg = ConversationMessage(
        conversation_id=conv.id,
        user_id=user.id,
        person_record_id=conv_record_id,
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
            person_record_id=conv_record_id,
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
        user_id=user.id,
        person_record_id=conv_record_id,
    )

    # FU-CAL-CONVERSATIONS-INTEGRATION (2026-05-22) — pull projected
    # calendar life-context for this record. The projector enforces
    # the two-elevation floor (privacy_mode + llm_full_details_consent)
    # per source; per-source history_window_back clamps how far back
    # events are exposed. Same retrieval contract as /api/ask, just
    # now in the user-facing Chat path.
    calendar_items: list[dict[str, Any]] = []
    if conv_record_id is not None:
        calendar_items = await fetch_calendar_life_context(
            db, person_record_id=conv_record_id,
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
    # Build a fact_id → source-meta map for evidence-block provenance.
    # P0-3 (2026-05-15): without source_label / filename / quality
    # tier visible to the LLM, Ask can't distinguish a primary
    # specialty record from a copy-forward pre-op surgical history.
    fact_source_meta: dict[uuid.UUID, dict[str, Any]] = {}
    if facts:
        from ..models.evidence_anchor import EvidenceAnchor
        # Anchor id → fact id (1:1 in practice; we read the first
        # anchor per fact as the "primary citation").
        anchor_to_fact: dict[uuid.UUID, uuid.UUID] = {}
        for ff in facts:
            if ff.evidence_anchor_ids:
                anchor_to_fact[ff.evidence_anchor_ids[0]] = ff.id
        if anchor_to_fact:
            rows = list((await db.execute(
                select(EvidenceAnchor.id, EvidenceAnchor.source_document_id)
                .where(EvidenceAnchor.id.in_(list(anchor_to_fact.keys())))
            )).all())
            anchor_to_source: dict[uuid.UUID, uuid.UUID] = {
                aid: sid for (aid, sid) in rows if sid is not None
            }
            source_by_id = {s.id: s for s in sources}
            for aid, fid in anchor_to_fact.items():
                sid = anchor_to_source.get(aid)
                src = source_by_id.get(sid) if sid else None
                if src is None:
                    continue
                # Prefer the persisted tier from ingest-time stamping
                # if present; fall back to classifying on demand for
                # legacy source rows that haven't been backfilled.
                tier = None
                if isinstance(src.raw_metadata, dict):
                    tier = src.raw_metadata.get("authority_tier")
                if not tier:
                    tier = _source_quality_tier(
                        src.source_label, src.original_filename, src.source_type,
                    )
                fact_source_meta[fid] = {
                    "source_label": src.source_label,
                    "original_filename": src.original_filename,
                    "source_quality": tier,
                    "authority_tier": tier,  # explicit doctrine name
                    "tier_rank": _TIER_RANK.get(tier, 4),
                }
    prompt = get_registry().get("general_ask")
    fact_block = _evidence_block(facts, fact_source_meta)
    calendar_block = format_calendar_context_block(calendar_items)
    evidence_block = fact_block + calendar_block

    # Count-only retrieval-shape telemetry per PM directive
    # (FU-CAL-CONVERSATIONS-INTEGRATION 2026-05-22). Mirrors the
    # ask_retrieval_shape event on /api/ask. NEVER log titles,
    # event ids, fact ids, prompt body, or answer text — only the
    # counts and char-lengths below.
    fact_type_counts: dict[str, int] = {}
    extraction_method_counts: dict[str, int] = {}
    for f in facts:
        fact_type_counts[f.fact_type] = fact_type_counts.get(f.fact_type, 0) + 1
        em = f.extraction_method or "(none)"
        extraction_method_counts[em] = extraction_method_counts.get(em, 0) + 1
    log.info(
        "conversations_retrieval_shape",
        conversation_id=str(conv.id),
        person_record_id=str(conv_record_id) if conv_record_id else None,
        scope_type=(conv.scope or {}).get("type"),
        fact_count=len(facts),
        fact_type_counts=fact_type_counts,
        extraction_method_counts=extraction_method_counts,
        calendar_item_count=len(calendar_items),
        fact_block_chars=len(fact_block),
        calendar_block_chars=len(calendar_block),
        context_block_chars=len(evidence_block),
        calendar_block_present=len(calendar_block) > 0,
    )

    result = await call_with_tool(
        db,
        user,
        prompt,
        user_vars={
            "question": content,
            "scope_description": _scope_description(conv.scope or {}),
            "evidence_count": str(len(facts)),
            "evidence_block": evidence_block,
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
        person_record_id=conv_record_id,
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
    #
    # M02 perimeter (Batch 5): citations the LLM emits must point at
    # subjects that live on the active record. We pre-compute the
    # in-scope `fact` and `source` id sets from the evidence we just
    # gathered and drop any citation whose subject_id isn't in it.
    # Other citation types (`anchor`, `episode`, `candidate`, `event`)
    # pass through — they don't surface across records in V1, but if
    # a future LLM emits one we don't silently insert it without
    # being scoped. Validate those separately when we light them up.
    in_scope_fact_ids = {f.id for f in facts}
    in_scope_source_ids = {s.id for s in sources}
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
        if ctype == "fact" and subject_id not in in_scope_fact_ids:
            # Hallucinated or out-of-record fact id — drop silently.
            continue
        if ctype == "source" and subject_id not in in_scope_source_ids:
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
