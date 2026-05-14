"""Claude extraction over clinical note plaintext (RTF/HTML/text).

The FHIR connector fetches DocumentReference attachments as RTF or HTML
and persists them as `source_documents` of type 'clinical_note'. Until
this module shipped, those notes' plaintext was sitting on disk with
`raw_metadata.has_plaintext=true` and zero ExtractedFact rows — so EI
told Nick "the anesthesia record didn't make it into your data" while
the Anesthesia Postprocedure Evaluation was sitting at /data/evidence/
naming the anesthesiologist.

Mirrors the extract_fax_vision pattern: tool-use enforcement, per-fact
anchors with text_excerpt, ModelRun audit row, review-state by
confidence. Differences: text input (no image base64), single one-shot
call per note (notes are <30k chars), anchor_type='note_section' so
the UI can render an inline "open the note here" link.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.logger import get_logger
from ..ingest.fact_classifier import review_state_for_vision
from ..llm import call_with_tool, get_registry
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.source_document import SourceDocument
from ..models.user import User

log = get_logger("ownchart.extract.clinical_note")


_CONFIDENCE_INT = {
    "high": 90,
    "medium": 65,
    "low": 35,
    "possible": 20,
    "unknown": None,
}

# Reuse the RTF / HTML strip patterns from fhir_attachments. RTF control
# words look like `\par`, `\f0`, `\fs24` etc.; we drop them aggressively
# and keep the prose. Won't render tables cleanly, but every Stanford
# / Hopkins / Bozeman note we've seen is prose, not tabular.
_RE_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")

# Hard cap on plaintext we ship to the LLM. Opus has 1M context but we
# stay sensible. Most clinical notes are 1–10k chars; this cap lets the
# rare 40-page operative note still get processed without exploding
# token costs.
_MAX_PLAINTEXT_CHARS = 30_000


@dataclass
class ClinicalNoteExtractionResult:
    source_id: uuid.UUID
    model_run_id: uuid.UUID | None
    fact_count: int
    error: str | None
    notes_to_reviewer: str | None


def _strip_to_plaintext(raw: bytes, mime: str | None) -> str:
    """Decode + strip RTF / HTML control codes. Returns whitespace-collapsed text."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    m = (mime or "").lower()
    if "html" in m or "xml" in m or "xhtml" in m:
        text = _RE_TAG.sub(" ", text)
    elif "rtf" in m or text.startswith("{\\rtf"):
        text = _RE_RTF_CONTROL.sub("", text)
        text = text.replace("{", "").replace("}", "")
    text = _RE_WS.sub(" ", text).strip()
    return text


def _date_from_emit(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    s = date_str.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _read_plaintext(source: SourceDocument) -> str:
    """Read the note's full plaintext.

    Order:
      1. Decode the file at storage_uri (the canonical RTF/HTML the
         connector saved). Strip control codes.
      2. Fall back to raw_metadata.plaintext_excerpt (capped at 2000
         chars by the connector — better than nothing).
      3. Return empty string. The caller will log + bail.
    """
    settings = get_settings()
    # storage_uri is "/data/evidence/<hash[:2]>/<hash[2:4]>/<hash>.ext"
    # inside the container; on the host it lives under OWNCHART_DATA_DIR.
    raw_path = (source.storage_uri or "").lstrip("/")
    # storage_uri stored is the container path "/data/...". Inside the
    # container the prefix IS /data; outside we'd need data_dir +
    # path[len('/data'):]. Try container path first, fall back to
    # local-dev rewrite.
    candidates: list[Path] = []
    if source.storage_uri:
        # As-is (container)
        candidates.append(Path(source.storage_uri))
        # data_dir rewrite (host / tests)
        try:
            data_dir = Path(settings.data_dir)
        except Exception:  # noqa: BLE001
            data_dir = None
        if data_dir is not None and source.storage_uri.startswith("/data/"):
            candidates.append(data_dir / source.storage_uri[len("/data/"):])

    raw_bytes: bytes | None = None
    for p in candidates:
        try:
            if p.exists():
                raw_bytes = p.read_bytes()
                break
        except OSError:
            continue

    if raw_bytes is not None:
        text = _strip_to_plaintext(raw_bytes, source.mime_type)
        if text:
            return text[:_MAX_PLAINTEXT_CHARS]

    # Fallback: the 2000-char excerpt the connector stashed.
    excerpt = ""
    if isinstance(source.raw_metadata, dict):
        excerpt = (source.raw_metadata.get("plaintext_excerpt") or "")
    return excerpt[:_MAX_PLAINTEXT_CHARS]


async def extract_clinical_note(
    db: AsyncSession,
    user: User,
    source: SourceDocument,
) -> ClinicalNoteExtractionResult:
    """Run the clinical-note extractor over ONE source_document.

    Idempotency: callers should check that no facts already anchor to
    this source before calling. We don't dedup against existing facts
    here — the caller's responsibility.
    """
    # ccda_xml is structurally the same problem: the FHIR connector
    # saves the document with has_plaintext=true and an HTML-strip-able
    # narrative, but no extractor ran. Reuse the same pipeline — the
    # plaintext strip below already handles html/xml/rtf.
    if source.source_type not in ("clinical_note", "ccda_xml"):
        return ClinicalNoteExtractionResult(
            source_id=source.id, model_run_id=None, fact_count=0,
            error=f"source_type {source.source_type} not supported by clinical-note extractor",
            notes_to_reviewer=None,
        )

    plaintext = _read_plaintext(source)
    if not plaintext or len(plaintext) < 40:
        # Not enough text to bother the LLM with. Mark as processed
        # so the backfill doesn't keep retrying.
        return ClinicalNoteExtractionResult(
            source_id=source.id, model_run_id=None, fact_count=0,
            error="plaintext empty or too short",
            notes_to_reviewer=None,
        )

    title = ""
    content_date = ""
    if isinstance(source.raw_metadata, dict):
        title = str(source.raw_metadata.get("title") or "")
        content_date = str(source.raw_metadata.get("creation") or "")

    prompt = get_registry().get("extract_clinical_note")
    result = await call_with_tool(
        db, user, prompt,
        user_vars={
            "note_title": title or source.original_filename or "(untitled)",
            "source_label": source.source_label or "(unlabeled)",
            "source_system": source.source_system or "(unknown)",
            "content_date": content_date or "(unknown)",
            "plaintext": plaintext,
        },
        purpose="extract_clinical_note",
        input_source_ids=[source.id],
        tool_name="emit_clinical_note_extraction",
        max_tokens=8192,
    )

    if result.error or not result.tool_input:
        return ClinicalNoteExtractionResult(
            source_id=source.id,
            model_run_id=result.model_run_id,
            fact_count=0,
            error=result.error or "no tool_input emitted",
            notes_to_reviewer=None,
        )

    emitted = result.tool_input

    # Parse a default date for facts the LLM didn't date explicitly —
    # use the note's content date.
    default_date: datetime | None = None
    if content_date:
        try:
            default_date = datetime.fromisoformat(content_date.replace("Z", "+00:00"))
        except ValueError:
            default_date = None

    pending: list[tuple[EvidenceAnchor, ExtractedFact]] = []
    fact_count = 0

    def add(fact_type: str, **kwargs: Any) -> None:
        nonlocal fact_count
        excerpt = (kwargs.get("text_excerpt") or "")
        anchor = EvidenceAnchor(
            source_document_id=source.id,
            anchor_type="note_section",
            page_number=None,
            text_excerpt=str(excerpt)[:2000] or None,
        )
        label = str(kwargs.get("label", ""))[:512] or "(unlabeled)"
        confidence_label = str(kwargs.get("confidence_label") or "").lower()
        review_state = review_state_for_vision(label, confidence_label)

        why_code: str | None = None
        why_text: str | None = None
        task_type: str | None = None
        source_only_eligible = False
        if review_state == "needs_review":
            if fact_type == "provider_relationship":
                why_code = "clinical_note_provider"
                why_text = (
                    "Provider name extracted from a clinical note's text. "
                    "Confirm or merge with an existing provider."
                )
                task_type = "provider_contact_cleanup"
                source_only_eligible = True
            else:
                why_code = "clinical_note_low_confidence"
                why_text = (
                    "Extracted from a clinical note by Claude; confidence "
                    f"reported as {confidence_label or 'unknown'}."
                )
                task_type = "confirm_event"

        fact = ExtractedFact(
            fact_type=fact_type,
            label=label,
            description=kwargs.get("description"),
            date_start=kwargs.get("date_start") or default_date,
            date_end=kwargs.get("date_end"),
            date_precision=kwargs.get("date_precision"),
            body_site=kwargs.get("body_site"),
            laterality=kwargs.get("laterality"),
            coded_concepts=kwargs.get("coded_concepts"),
            confidence=_CONFIDENCE_INT.get(confidence_label),
            review_state=review_state,
            evidence_anchor_ids=[],
            extraction_method="claude_clinical_note_v1",
            model_run_id=result.model_run_id,
            why_needs_review_code=why_code,
            why_needs_review_text=why_text,
            review_task_type=task_type,
            source_context_only_eligible=source_only_eligible,
        )
        pending.append((anchor, fact))
        fact_count += 1

    # Conditions
    for c in emitted.get("conditions", []) or []:
        add(
            "condition",
            label=c.get("label"),
            description=c.get("status"),
            body_site=c.get("body_site"),
            laterality=c.get("laterality"),
            date_start=_date_from_emit(c.get("date_observed")),
            date_precision=c.get("date_precision"),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Procedures
    for c in emitted.get("procedures", []) or []:
        add(
            "procedure",
            label=c.get("label"),
            description=c.get("provider"),
            body_site=c.get("body_site"),
            laterality=c.get("laterality"),
            date_start=_date_from_emit(c.get("date")),
            date_precision=c.get("date_precision"),
            confidence_label=c.get("confidence"),
            text_excerpt=c.get("text_excerpt"),
        )
    # Medications — intent goes into coded_concepts.intent + description.
    for c in emitted.get("medications", []) or []:
        dose = c.get("dose") or ""
        route = c.get("route") or ""
        freq = c.get("frequency") or ""
        intent = c.get("intent") or "unknown"
        desc_parts = [s for s in [dose, route, freq] if s]
        desc = " · ".join(desc_parts) if desc_parts else None
        add(
            "medication",
            label=c.get("label"),
            description=desc,
            date_start=_date_from_emit(c.get("date_started")),
            date_end=_date_from_emit(c.get("date_stopped")),
            confidence_label=c.get("confidence"),
            coded_concepts={"intent": intent} if intent else None,
            text_excerpt=c.get("text_excerpt"),
        )
    # Providers
    for p in emitted.get("providers", []) or []:
        bits = [s for s in [p.get("role"), p.get("specialty"), p.get("organization")] if s]
        add(
            "provider_relationship",
            label=p.get("name") or "(provider)",
            description=" | ".join(bits) if bits else None,
            text_excerpt=p.get("text_excerpt"),
        )
    # Instructions
    for i in emitted.get("instructions", []) or []:
        add(
            "instruction",
            label=i.get("label"),
            description=i.get("applies_to"),
            date_end=_date_from_emit(i.get("date_through")),
            confidence_label="high",  # quoted directly from clinician text
            text_excerpt=i.get("text_excerpt"),
        )
    # Findings
    for f in emitted.get("findings", []) or []:
        value = f.get("value") or ""
        unit = f.get("unit") or ""
        desc = " ".join(s for s in [value, unit] if s) or None
        add(
            "observation",
            label=f.get("label"),
            description=desc,
            date_start=_date_from_emit(f.get("date")),
            confidence_label="high",
            text_excerpt=f.get("text_excerpt"),
        )
    # Anesthesia block — pull out agents into individual medication facts
    # with intent=given_intraop so EI's anesthesia bucket lights up.
    anesthesia = emitted.get("anesthesia") or {}
    if isinstance(anesthesia, dict):
        anes_excerpt = anesthesia.get("text_excerpt") or ""
        for agent in anesthesia.get("agents", []) or []:
            if not isinstance(agent, str) or not agent.strip():
                continue
            add(
                "medication",
                label=agent.strip(),
                description="intraoperative anesthetic",
                date_start=default_date,
                confidence_label="high",
                coded_concepts={"intent": "given_intraop"},
                text_excerpt=anes_excerpt or agent,
            )
        # Anesthesiologist + CRNA → provider_relationship.
        for role_key, role_label in (
            ("anesthesiologist", "anesthesiologist"),
            ("crna", "CRNA"),
        ):
            name = anesthesia.get(role_key)
            if isinstance(name, str) and name.strip():
                add(
                    "provider_relationship",
                    label=name.strip(),
                    description=role_label,
                    text_excerpt=anes_excerpt or f"{role_label}: {name}",
                )
        # Technique / airway summary as a single instruction-style fact
        # so EI can quote it without re-parsing the note.
        tech = anesthesia.get("technique") or anesthesia.get("primary_anesthetic")
        if isinstance(tech, str) and tech.strip():
            add(
                "instruction",
                label=f"Anesthetic technique: {tech.strip()}",
                description=anesthesia.get("airway") or None,
                date_start=default_date,
                confidence_label="high",
                text_excerpt=anes_excerpt or tech,
            )
    # Discharge block — instructions, follow-ups, red flags.
    discharge = emitted.get("discharge") or {}
    if isinstance(discharge, dict):
        disch_excerpt = discharge.get("text_excerpt") or ""
        for key, applies_to in (
            ("activity_restrictions", "activity"),
            ("wound_care", "wound_care"),
            ("diet", "diet"),
            ("disposition", "other"),
        ):
            val = discharge.get(key)
            if isinstance(val, str) and val.strip():
                add(
                    "instruction",
                    label=f"{key.replace('_', ' ').title()}: {val.strip()[:200]}",
                    description=applies_to,
                    date_start=default_date,
                    confidence_label="high",
                    text_excerpt=disch_excerpt or val,
                )
        for appt in discharge.get("follow_up_appointments", []) or []:
            if not isinstance(appt, dict):
                continue
            with_ = (appt.get("with") or "").strip()
            when = (appt.get("when") or "").strip()
            reason = (appt.get("reason") or "").strip()
            if not (with_ or when or reason):
                continue
            add(
                "instruction",
                label=f"Follow-up: {with_ or '(provider)'} — {when or 'date TBD'}",
                description=reason or "follow_up",
                date_start=_date_from_emit(when) or default_date,
                confidence_label="high",
                text_excerpt=disch_excerpt or f"{with_} {when} {reason}",
            )
        for rf in discharge.get("red_flags", []) or []:
            if not isinstance(rf, str) or not rf.strip():
                continue
            add(
                "instruction",
                label=f"Call about: {rf.strip()[:200]}",
                description="red_flag",
                date_start=default_date,
                confidence_label="high",
                text_excerpt=disch_excerpt or rf,
            )

    # Persist: anchors first to get IDs, then attach to facts, then commit.
    for anchor, _fact in pending:
        db.add(anchor)
    if pending:
        await db.flush()
        for anchor, fact in pending:
            fact.evidence_anchor_ids = [anchor.id]
            db.add(fact)

    # Stamp the source so the backfill / inbox knows we processed it.
    rm = dict(source.raw_metadata or {})
    rm["extraction_status"] = "completed"
    rm["extraction_run_at"] = datetime.now(timezone.utc).isoformat()
    rm["extraction_fact_count"] = fact_count
    rm["extraction_method"] = "claude_clinical_note_v1"
    if emitted.get("notes_to_reviewer"):
        rm["extraction_notes_to_reviewer"] = emitted["notes_to_reviewer"]
    source.raw_metadata = rm

    await db.commit()

    log.info(
        "clinical_note_extracted",
        source_id=str(source.id),
        fact_count=fact_count,
        model_run_id=str(result.model_run_id) if result.model_run_id else None,
        note_title=title,
    )

    return ClinicalNoteExtractionResult(
        source_id=source.id,
        model_run_id=result.model_run_id,
        fact_count=fact_count,
        error=None,
        notes_to_reviewer=(emitted.get("notes_to_reviewer") or None),
    )
