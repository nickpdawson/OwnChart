"""SourceDocument endpoints.

Phase 2.A ships:
  POST /api/sources/photo  — multipart upload of a single image
  GET  /api/sources         — list (already in Phase 1)
  GET  /api/sources/{id}    — fetch one
  GET  /api/sources/{id}/thumb/{size} — serve thumbnail (sm | md)

PDF / CCDA / FHIR / Auto Export lanes land in 2.B.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.consent import require_phi_consent
from ..core.db import get_session
from ..core.logger import get_logger
from ..core.upload_context import attach_nearby_clinical_events
from ..ingest import auto_export as auto_export_ingest
from ..ingest import ccda as ccda_ingest
from ..ingest import images, pdf, storage
from ..ingest.fact_classifier import review_state_for_fhir
from ..core.arq_pool import enqueue_extraction_job, enqueue_personal_photo_vision
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.extraction_job import ExtractionJob
from ..models.source_document import SourceDocument
from ..models.user import User
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.sources")


class SourceSummary(BaseModel):
    id: str
    source_type: str
    original_filename: str | None
    source_label: str | None
    captured_at: datetime | None
    user_supplied_event_date: datetime | None
    user_supplied_caption: str | None


class SourceDetail(SourceSummary):
    storage_uri: str
    hash: str
    mime_type: str | None
    acquired_at: datetime
    raw_metadata: dict | None
    exif_metadata: dict | None
    has_gps: bool
    # Surface the background-extraction state at the top level so the
    # UI doesn't have to dig into raw_metadata. Nick RC review 2026-05-14:
    # "the user/admin must be able to see when sync succeeds but
    # extraction fails 30 seconds later."
    extraction_status: str | None = None      # "completed" | "failed" | "skipped" | "pending" | None
    extraction_fact_count: int | None = None
    extraction_error: str | None = None
    extraction_run_at: datetime | None = None
    extraction_failed_at: datetime | None = None
    # Photo-vision lifecycle (2026-05-16). Distinct from
    # extraction_*: this covers Claude vision on personal-photo
    # uploads (camera-roll), not the clinical-note extractor that
    # runs on EHR-fetched RTF/HTML/CCDA.
    #
    #   vision_status: uploaded | analysis_queued | analysis_complete | analysis_failed | none
    #   vision_structured_fact_count: number of structured_facts
    #       persisted from a screenshot (vaccine card, lab result,
    #       prescription label, etc.). Zero on body/device/setting
    #       photos — those produce a description but no structured
    #       fact rows.
    #   vision_relevance_score: 0–100, the model's call on how
    #       clinically relevant the photo is. <30 hides from
    #       clinical retrieval.
    vision_status: str | None = None
    vision_structured_fact_count: int | None = None
    vision_relevance_score: int | None = None


def _to_summary(s: SourceDocument) -> SourceSummary:
    return SourceSummary(
        id=str(s.id),
        source_type=s.source_type,
        original_filename=s.original_filename,
        source_label=s.source_label,
        captured_at=s.captured_at,
        user_supplied_event_date=s.user_supplied_event_date,
        user_supplied_caption=s.user_supplied_caption,
    )


@router.get("")
async def list_sources(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[SourceSummary]:
    result = await db.execute(select(SourceDocument).order_by(SourceDocument.acquired_at.desc()))
    return [_to_summary(s) for s in result.scalars().all()]


@router.get("/{source_id}")
async def get_source(
    source_id: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    # Accept UUID prefixes (>=8 chars) so citation chips like
    # `[source:affdb681]` from the LLM resolve to a real row instead
    # of 422-ing the Next.js server render. The chip emitter (see
    # ThreadClient.tsx CITATION_PATTERN) captures 6-36 hex chars,
    # so we mirror that range here. Full UUIDs still take the fast
    # path via db.get; prefixes do an indexed text-LIKE lookup.
    sid = (source_id or "").strip()
    if not sid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    src: SourceDocument | None = None
    try:
        src = await db.get(SourceDocument, uuid.UUID(sid))
    except ValueError:
        # Not a full UUID — try prefix resolution.
        if len(sid) < 8 or not all(c in "0123456789abcdefABCDEF-" for c in sid):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        from sqlalchemy import text as _text
        like_pat = f"{sid.lower()}%"
        matches = list((await db.execute(
            select(SourceDocument)
            .where(_text("cast(id as text) like :pat").bindparams(pat=like_pat))
            .limit(2)
        )).scalars().all())
        if len(matches) == 1:
            src = matches[0]
        # 0 or >1 → fall through to 404 below
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    has_gps = bool((src.exif_metadata or {}).get("GPSInfo"))
    rm = src.raw_metadata or {}

    def _parse_dt(v: Any) -> datetime | None:
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    # Derive vision-pipeline state from raw_metadata for photos.
    # `vision_status`:
    #   - "uploaded"          → photo persisted but no vision data
    #                           (either bulk-import not yet analyzed
    #                            or non-photo source type)
    #   - "analysis_queued"   → vision_pending flag set, no vision
    #                           data yet
    #   - "analysis_complete" → raw_metadata.vision is populated
    #   - "analysis_failed"   → raw_metadata.vision.error set
    vision_blob = rm.get("vision") if isinstance(rm.get("vision"), dict) else None
    if src.source_type != "photo":
        vision_status = None
        vision_sf = None
        vision_relev = None
    elif vision_blob is None:
        vision_status = "analysis_queued" if rm.get("vision_pending") else "uploaded"
        vision_sf = None
        vision_relev = None
    elif vision_blob.get("error"):
        vision_status = "analysis_failed"
        vision_sf = vision_blob.get("structured_fact_count")
        vision_relev = vision_blob.get("relevance_score")
    else:
        vision_status = "analysis_complete"
        vision_sf = vision_blob.get("structured_fact_count") or 0
        vision_relev = vision_blob.get("relevance_score")

    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=has_gps,
        extraction_status=rm.get("extraction_status"),
        extraction_fact_count=rm.get("extraction_fact_count"),
        extraction_error=rm.get("extraction_error"),
        extraction_run_at=_parse_dt(rm.get("extraction_run_at")),
        extraction_failed_at=_parse_dt(rm.get("extraction_failed_at")),
        vision_status=vision_status,
        vision_structured_fact_count=vision_sf,
        vision_relevance_score=vision_relev,
    )


# Photo upload safeguards (2026-05-13 PM):
# - Files smaller than this are almost always thumbnails, icons, or
#   accidental empty captures. Reject with a clear 415 rather than
#   spending vision tokens on them.
_PHOTO_MIN_BYTES = 8 * 1024  # 8 KB
# - Bulk camera-roll imports should NOT auto-trigger Claude vision
#   per photo. Vision spend on 200 vacation photos is wasted; user
#   wants to opt in via an explicit "Analyze these" action. iOS sends
#   batch_import=true when the photo came from a multi-pick gesture.
# - Intentional single-photo uploads (camera button, "add a photo of
#   …") keep auto-vision on the V1 path.


@router.post("/photo", status_code=status.HTTP_201_CREATED)
async def upload_photo(
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    event_date: datetime | None = Form(default=None),
    source_label: str | None = Form(default=None),
    batch_import: bool = Form(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    # Reliability doctrine (alpha P0, 2026-05-15): every error path must
    # return structured JSON with a user-safe `detail` and log the real
    # exception server-side. Bare 500s from uvicorn (the 21-byte plain
    # "Internal Server Error" body) leave iOS unable to decode anything
    # and the user sees only "Server error 500". Catch broadly, log
    # narrowly.
    if file.content_type and file.content_type not in images.SUPPORTED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content-type: {file.content_type}",
        )

    # Buffer once for hashing + thumbnail generation. Photos are typically
    # under 50MB; if we ever stream larger media we'll switch to spooled
    # tempfiles. Worth revisiting before DICOM.
    try:
        raw = await file.read()
    except Exception as e:  # noqa: BLE001
        log.exception("photo_upload_read_failed", filename=file.filename)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Couldn't read uploaded file: {e.__class__.__name__}",
        ) from e
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file",
        )
    if len(raw) < _PHOTO_MIN_BYTES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Image is {len(raw)} bytes — too small to be a useful "
                "health photo (likely a thumbnail or icon). Send the "
                "full-resolution image, or upload as a note instead."
            ),
        )

    # Persist raw bytes content-addressed.
    suffix = Path(file.filename or "").suffix.lower()

    async def _stream():
        yield raw

    try:
        blob = await storage.write_blob(_stream(), suffix=suffix)
    except OSError as e:
        log.exception("photo_upload_storage_failed",
                      filename=file.filename, size_bytes=len(raw))
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=f"Couldn't write to evidence vault: {e.__class__.__name__}",
        ) from e

    # Allocate the SourceDocument id BEFORE thumbnailing so renders dir is
    # keyed by it.
    src_id = uuid.uuid4()
    try:
        meta = images.analyze_and_thumbnail(raw, str(src_id))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(e),
        ) from e
    except OSError as e:
        # PIL's UnidentifiedImageError (subclass of OSError) and HEIC
        # decode failures land here. So do disk-write failures during
        # thumbnail save. Treat decode-class as 415, disk-class as 507.
        log.exception("photo_upload_image_processing_failed",
                      filename=file.filename, size_bytes=len(raw),
                      content_type=file.content_type)
        # Heuristic: errno set → real I/O error; no errno → PIL identify
        if getattr(e, "errno", None):
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail=f"Couldn't process image: {e.__class__.__name__}",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Couldn't decode this image. The file may be corrupt or "
                "an unsupported variant (e.g. HEIC depth or burst). "
                "Try a JPEG export."
            ),
        ) from e
    except Exception as e:  # noqa: BLE001
        log.exception("photo_upload_image_unexpected_failure",
                      filename=file.filename, size_bytes=len(raw),
                      content_type=file.content_type)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Image processing failed: {e.__class__.__name__}",
        ) from e

    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="photo",
        original_filename=file.filename,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type=file.content_type,
        acquired_at=datetime.now(timezone.utc),
        source_system="patient_upload",
        source_label=source_label,
        raw_metadata={
            "format": meta.pil_format,
            "width": meta.width,
            "height": meta.height,
            "thumbnails": meta.thumbnails,
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
            # batch_import=true means iOS imported this from a multi-
            # pick camera-roll gesture; defer auto-vision to an
            # explicit "Analyze these" action via POST /sources/{id}/analyze.
            # batch_import=false (default) keeps the intentional single-
            # upload path on auto-vision.
            "batch_import": batch_import,
            "vision_pending": batch_import,  # true if vision is deferred
        },
        captured_at=meta.captured_at,
        exif_metadata=meta.exif or None,
        user_supplied_event_date=event_date,
        user_supplied_caption=caption,
    )
    db.add(src)
    await db.flush()

    # If the user gave the photo any context (caption or event_date), create
    # a confirmed life_context_event fact so the photo lands on dossiers
    # via the same retrieval as notes. Untagged photos stay in the vault
    # only — they don't pollute topic timelines.
    photo_date = event_date or meta.captured_at
    if caption or photo_date:
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            anchor_type="image_full",
            text_excerpt=caption[:2000] if caption else None,
        )
        db.add(anchor)
        await db.flush()
        claim_label = caption or file.filename or "photo"
        fact = ExtractedFact(
            fact_type="life_context_event",
            label=claim_label[:512],
            description=caption,
            date_start=photo_date,
            date_end=None,
            date_precision="day" if photo_date else None,
            confidence=100,
            review_state="confirmed",
            evidence_anchor_ids=[anchor.id],
            extraction_method="patient_self_report",
        )
        db.add(fact)

    # Auto-association: pin nearby major clinical events onto the
    # source so the source detail page can show "this photo is on the
    # same day as your appendectomy / fibula fracture" without the
    # user manually associating it. Runs synchronously — it's one DB
    # query against confirmed facts in a ±7 day window.
    #
    # Reliability: failure here must NOT fail the upload. The photo +
    # blob + fact have already been written; nearby_clinical_events is
    # a UI nicety. Log the exception, set the field to [], move on.
    try:
        nearby = await attach_nearby_clinical_events(db, user, src)
    except Exception as e:  # noqa: BLE001
        log.exception("photo_upload_nearby_events_failed",
                      source_id=str(src_id))
        nearby = []
        # Don't leave the session dirty from a half-applied query.
        try:
            raw_meta = dict(src.raw_metadata or {})
            raw_meta["nearby_clinical_events"] = []
            raw_meta["nearby_clinical_events_error"] = e.__class__.__name__
            src.raw_metadata = raw_meta
        except Exception:  # noqa: BLE001
            pass

    try:
        await db.commit()
        await db.refresh(src)
    except Exception as e:  # noqa: BLE001
        log.exception("photo_upload_commit_failed", source_id=str(src_id))
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Couldn't save photo metadata: {e.__class__.__name__}",
        ) from e

    # Fire-and-forget Claude vision over the photo — unless this is a
    # bulk camera-roll import (batch_import=true). For batched imports
    # the photo lands in the vault but vision is deferred until the
    # user explicitly triggers it via POST /sources/{id}/analyze.
    # raw_metadata.vision_pending=true is the UI's signal that the
    # photo is awaiting analysis.
    if not batch_import:
        try:
            await enqueue_personal_photo_vision(str(src.id))
        except Exception as e:  # noqa: BLE001
            log.warning("photo_vision_enqueue_failed",
                        source_id=str(src.id), error=str(e))

    log.info(
        "photo_uploaded",
        source_id=str(src.id),
        format=meta.pil_format,
        width=meta.width,
        height=meta.height,
        has_gps=meta.has_gps,
        deduplicated=blob.already_existed,
        captioned=bool(caption),
        dated=bool(photo_date),
        nearby_clinical_events=len(nearby),
    )

    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=meta.has_gps,
    )


# ---------------------------------------------------------------------------
# Personal-lane uploads — note + voice memo (Upload tab on iOS)
# ---------------------------------------------------------------------------
#
# These mirror the /photo endpoint's contract: store the raw artifact +
# generate a confirmed life_context_event fact at the supplied event_date so
# it lands on the timeline / dossier surfaces via the same retrieval path as
# captioned photos. The artifact stays the canonical evidence; the fact is
# just the index entry so date-proximity clustering works ("photo of the day
# I broke my ankle" sits next to the 2023-07-15 fracture facts).
#
# STT model: iOS sends the audio file PLUS an on-device transcript
# (Speech framework) when permission is granted. The transcript becomes the
# fact's description and participates in search_facts retrieval. The raw
# audio is stored unmodified so a future server-side Whisper pass can
# re-transcribe if needed. If iOS posts audio without a transcript, the
# source is stored but no fact is created — V1.1 will add a transcription
# worker that fills in retroactively.


class NoteCreate(BaseModel):
    body: str
    title: str | None = None
    event_date: datetime | None = None
    source_label: str | None = None


@router.post("/note", status_code=status.HTTP_201_CREATED)
async def upload_note(
    payload: NoteCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    """Typed note from the iOS Upload tab.

    Creates a SourceDocument(source_type='note') + a confirmed
    life_context_event fact at `event_date` (defaults to now) so the note
    participates in timeline and dossier retrieval the moment it lands.
    """
    body = payload.body.strip()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty note body",
        )
    if len(body) > 50_000:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Note body exceeds 50KB; split into multiple notes",
        )

    raw_bytes = body.encode("utf-8")

    async def _stream():
        yield raw_bytes

    blob = await storage.write_blob(_stream(), suffix=".txt")
    src_id = uuid.uuid4()
    event_at = payload.event_date or datetime.now(timezone.utc)

    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="note",
        original_filename=(payload.title or "note") + ".txt",
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type="text/plain",
        acquired_at=datetime.now(timezone.utc),
        source_system="patient_upload",
        source_label=payload.source_label or payload.title,
        raw_metadata={
            "title": payload.title,
            "char_count": len(body),
            "byte_count": len(raw_bytes),
            "deduplicated": blob.already_existed,
        },
        captured_at=payload.event_date,
        user_supplied_event_date=payload.event_date,
    )
    db.add(src)
    await db.flush()

    anchor = EvidenceAnchor(
        source_document_id=src.id,
        anchor_type="note_full",
        text_excerpt=body[:2000],
    )
    db.add(anchor)
    await db.flush()

    label = (payload.title or body.split("\n", 1)[0])[:512]
    fact = ExtractedFact(
        fact_type="life_context_event",
        label=label,
        description=body[:4000],
        date_start=event_at,
        date_end=None,
        date_precision="day",
        confidence=100,
        review_state="confirmed",
        evidence_anchor_ids=[anchor.id],
        extraction_method="patient_self_report",
    )
    db.add(fact)
    # Auto-association — same pattern as photo upload.
    nearby = await attach_nearby_clinical_events(db, user, src)
    await db.commit()
    await db.refresh(src)

    log.info(
        "note_uploaded",
        source_id=str(src.id),
        char_count=len(body),
        dated=bool(payload.event_date),
        titled=bool(payload.title),
        nearby_clinical_events=len(nearby),
    )

    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=None,
        has_gps=False,
    )


_AUDIO_MIME_PREFIXES = ("audio/",)
_VOICE_MAX_BYTES = 50 * 1024 * 1024  # 50MB; ~80 min of m4a


@router.post("/voice", status_code=status.HTTP_201_CREATED)
async def upload_voice(
    file: UploadFile = File(...),
    transcript: str | None = Form(default=None),
    title: str | None = Form(default=None),
    event_date: datetime | None = Form(default=None),
    source_label: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    """Voice memo from the iOS Upload tab.

    iOS does on-device transcription via Apple's Speech framework when
    permission is granted and sends the resulting text as `transcript`.
    The audio file is the canonical evidence; the transcript is the
    indexable form. If `transcript` is omitted, the audio is stored but
    no fact is generated — a future V1.1 worker will run server-side
    Whisper on un-transcribed voice sources and backfill.
    """
    if not file.content_type or not file.content_type.startswith(_AUDIO_MIME_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content-type: {file.content_type}; expected audio/*",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio file",
        )
    if len(raw) > _VOICE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Audio exceeds {_VOICE_MAX_BYTES // (1024 * 1024)}MB",
        )

    suffix = Path(file.filename or "").suffix.lower() or ".m4a"

    async def _stream():
        yield raw

    blob = await storage.write_blob(_stream(), suffix=suffix)
    src_id = uuid.uuid4()
    event_at = event_date or datetime.now(timezone.utc)
    transcript_text = (transcript or "").strip()

    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="voice_memo",
        original_filename=file.filename,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type=file.content_type,
        acquired_at=datetime.now(timezone.utc),
        source_system="patient_upload",
        source_label=source_label or title,
        raw_metadata={
            "title": title,
            "transcript": transcript_text or None,
            "has_transcript": bool(transcript_text),
            "size_bytes": blob.size_bytes,
            "deduplicated": blob.already_existed,
        },
        captured_at=event_date,
        user_supplied_event_date=event_date,
    )
    db.add(src)
    await db.flush()

    # Only create a fact if we have content to index. Bare audio without a
    # transcript stays in the vault until server-side STT runs (V1.1).
    if transcript_text:
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            anchor_type="voice_transcript",
            text_excerpt=transcript_text[:2000],
        )
        db.add(anchor)
        await db.flush()

        label = (title or transcript_text.split("\n", 1)[0])[:512]
        fact = ExtractedFact(
            fact_type="life_context_event",
            label=label,
            description=transcript_text[:4000],
            date_start=event_at,
            date_end=None,
            date_precision="day",
            confidence=95,
            review_state="confirmed",
            evidence_anchor_ids=[anchor.id],
            extraction_method="patient_self_report",
        )
        db.add(fact)

    # Auto-association — same pattern as photo / note upload.
    nearby = await attach_nearby_clinical_events(db, user, src)

    await db.commit()
    await db.refresh(src)

    log.info(
        "voice_uploaded",
        source_id=str(src.id),
        size_bytes=blob.size_bytes,
        has_transcript=bool(transcript_text),
        transcript_chars=len(transcript_text),
        dated=bool(event_date),
        nearby_clinical_events=len(nearby),
    )

    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=None,
        has_gps=False,
    )


@router.post("/pdf", status_code=status.HTTP_201_CREATED)
async def upload_pdf(
    file: UploadFile = File(...),
    source_label: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    if file.content_type and file.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content-type: {file.content_type}",
        )
    raw = await file.read()
    if not raw or raw[:5] != b"%PDF-":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a PDF")

    suffix = ".pdf"

    async def _stream():
        yield raw

    blob = await storage.write_blob(_stream(), suffix=suffix)
    src_id = uuid.uuid4()

    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="pdf",  # process_pdf_source flips to fax_pdf if appropriate
        original_filename=file.filename,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type=file.content_type or "application/pdf",
        acquired_at=datetime.now(timezone.utc),
        source_system="patient_upload",
        source_label=source_label,
        raw_metadata={
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
        },
    )
    db.add(src)
    await db.flush()

    # Render pages, create per-page anchors, run Tesseract OCR if no text layer.
    pdf_stats = await pdf.process_pdf_source(db, src, raw, refine_source_type=True)

    await db.commit()
    await db.refresh(src)

    log.info(
        "pdf_uploaded",
        source_id=str(src.id),
        page_count=pdf_stats["page_count"],
        any_text=pdf_stats["any_page_has_text"],
        ocr_ran=pdf_stats["ocr_ran"],
    )
    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=False,
    )


class CcdaImportItem(BaseModel):
    filename: str | None
    status: str  # 'parsed' | 'skipped' | 'error'
    source_id: str | None = None
    fact_count: int | None = None
    reason: str | None = None  # populated for skipped / error


class CcdaImportSummary(BaseModel):
    documents_found: int
    parsed: int
    skipped: int
    errors: int
    total_facts_created: int
    items: list[CcdaImportItem]


def _looks_like_ccda(raw: bytes) -> bool:
    head = raw[:300].lower()
    return b"<?xml" in head or b"<clinicaldocument" in head


async def _ingest_one_ccda(
    db: AsyncSession,
    user: User,
    raw: bytes,
    filename: str | None,
    mime_type: str | None,
    source_label: str | None,
    parent_source_document_id: uuid.UUID | None,
) -> tuple[SourceDocument, int]:
    """Ingest a single CCDA XML's bytes. Caller commits.

    Reused by both single-file and multi-file upload paths, and the
    structural seam where future bundle/archive ingestion (unzip →
    iterate) plugs in: an unzipper just needs to call this for each
    inner XML with the parent archive's source_id.

    Raises if the bytes don't parse as CCDA. Caller decides how to
    surface that — single-file path raises HTTP 422; multi-file path
    catches and reports per-file in the import summary.
    """
    parsed = ccda_ingest.parse_ccda(raw)

    async def _stream():
        yield raw

    blob = await storage.write_blob(_stream(), suffix=".xml")

    src = SourceDocument(
        id=uuid.uuid4(),
        owner_user_id=user.id,
        parent_source_document_id=parent_source_document_id,
        source_type="ccda_xml",
        original_filename=filename,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type=mime_type or "application/xml",
        acquired_at=datetime.now(timezone.utc),
        source_system=parsed.document_title or "ccda",
        source_label=source_label,
        raw_metadata={
            "patient_name": parsed.patient_name,
            "patient_dob": parsed.patient_dob.isoformat() if parsed.patient_dob else None,
            "document_title": parsed.document_title,
            "document_effective": parsed.document_effective.isoformat() if parsed.document_effective else None,
            "fact_count": len(parsed.facts),
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
        },
    )
    db.add(src)
    await db.flush()

    for cc in parsed.facts:
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            anchor_type="ccda_section",
            section_path=cc.section_path,
            text_excerpt=(cc.text_excerpt or "")[:2000] or None,
        )
        db.add(anchor)
        await db.flush()

        fact = ExtractedFact(
            fact_type=cc.fact_type,
            label=cc.label,
            description=cc.description,
            date_start=cc.date_start,
            date_end=cc.date_end,
            date_precision=cc.date_precision,
            coded_concepts=cc.coded_concepts or None,
            confidence=cc.confidence,
            # CCDA is provider-attested like FHIR — auto-classify the same way:
            # template noise → 'deferred', clinical content → 'confirmed'.
            review_state=review_state_for_fhir(cc.label, cc.description),
            evidence_anchor_ids=[anchor.id],
            extraction_method="ccda_xpath",
        )
        db.add(fact)

    return src, len(parsed.facts)


@router.post("/ccda", status_code=status.HTTP_201_CREATED)
async def upload_ccda(
    files: list[UploadFile] = File(...),
    source_label: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> CcdaImportSummary:
    """Multi-file CCDA ingest.

    Accepts one or more XML files in a single request. Each file is
    parsed and stored as its own SourceDocument with anchors and
    extracted facts; failures and skips are reported per-file in the
    summary rather than failing the whole batch. This is the seam
    Epic IHE_XDM bundle ingestion will plug into (the eventual
    bundle endpoint unzips → calls `_ingest_one_ccda` for each
    inner XML, with the parent archive's source_id).

    Per-file commit so partial success survives — if file 5 of 11
    fails, files 1–4 are persisted and the user sees what to retry.
    """
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files")

    items: list[CcdaImportItem] = []
    parsed_n = 0
    skipped_n = 0
    error_n = 0
    total_facts = 0

    for f in files:
        raw = await f.read()
        fname = f.filename
        if not raw:
            items.append(CcdaImportItem(
                filename=fname, status="skipped", reason="empty file",
            ))
            skipped_n += 1
            continue
        if not _looks_like_ccda(raw):
            items.append(CcdaImportItem(
                filename=fname, status="skipped",
                reason="not an XML/CCDA document",
            ))
            skipped_n += 1
            continue

        try:
            src, fact_count = await _ingest_one_ccda(
                db, user, raw,
                filename=fname,
                mime_type=f.content_type,
                source_label=source_label,
                parent_source_document_id=None,
            )
            await db.commit()
            await db.refresh(src)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            log.warning("ccda_ingest_failed", filename=fname, error=str(e))
            items.append(CcdaImportItem(
                filename=fname, status="error", reason=f"parse/ingest failed: {e}",
            ))
            error_n += 1
            continue

        items.append(CcdaImportItem(
            filename=fname, status="parsed",
            source_id=str(src.id), fact_count=fact_count,
        ))
        parsed_n += 1
        total_facts += fact_count
        log.info("ccda_ingested", source_id=str(src.id), fact_count=fact_count)

    return CcdaImportSummary(
        documents_found=len(files),
        parsed=parsed_n,
        skipped=skipped_n,
        errors=error_n,
        total_facts_created=total_facts,
        items=items,
    )


class NoteUpload(BaseModel):
    body: str
    title: str | None = None
    occurred_at: datetime | None = None
    body_site: str | None = None
    laterality: str | None = None  # left | right | bilateral | unknown


@router.post("/note", status_code=status.HTTP_201_CREATED)
async def upload_note(
    body: NoteUpload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    if not body.body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty note")

    text_bytes = body.body.encode("utf-8")

    async def _stream():
        yield text_bytes

    blob = await storage.write_blob(_stream(), suffix=".txt")
    src_id = uuid.uuid4()

    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="note",
        original_filename=(body.title + ".txt") if body.title else None,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type="text/plain",
        acquired_at=datetime.now(timezone.utc),
        source_system="patient_self_report",
        source_label=body.title,
        raw_metadata={"deduplicated": blob.already_existed, "size_bytes": blob.size_bytes},
        user_supplied_event_date=body.occurred_at,
        user_supplied_caption=body.title,
    )
    db.add(src)
    await db.flush()

    anchor = EvidenceAnchor(
        source_document_id=src.id,
        anchor_type="note_full",
        text_excerpt=body.body[:2000],
    )
    db.add(anchor)
    await db.flush()

    fact = ExtractedFact(
        fact_type="life_context_event",
        label=body.title or body.body[:120],
        description=body.body,
        date_start=body.occurred_at,
        date_end=None,
        date_precision="day" if body.occurred_at else None,
        body_site=body.body_site,
        laterality=body.laterality,
        confidence=100,
        review_state="confirmed",
        evidence_anchor_ids=[anchor.id],
        extraction_method="patient_self_report",
    )
    db.add(fact)

    await db.commit()
    await db.refresh(src)
    log.info("note_uploaded", source_id=str(src.id))
    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=False,
    )


@router.post("/auto-export", status_code=status.HTTP_201_CREATED)
async def upload_auto_export(
    file: UploadFile = File(...),
    source_label: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    """Ingest a Health Auto Export payload (JSON for V1).

    Per docs/03 Lane 4 + the locked-in V1 decision in docs/05:
    Health Auto Export iOS app first; HealthKit XML archive importer
    is a follow-up. Each metric becomes an ExtractedFact with
    extraction_method='health_auto_export', confidence=95,
    review_state='confirmed' (sensor data is high-trust at the
    value level; user can correct anything).

    Lab-shaped HealthKit quantities are intentionally skipped — labs
    belong in the clinical lane (FHIR/CCDA), not the wearable lane.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    fname = (file.filename or "").lower()
    is_json = fname.endswith(".json") or (file.content_type or "").startswith("application/json")
    if not is_json:
        # CSV-per-metric path lands later. For V1 require JSON which
        # is the format the iOS app's "Export → JSON" produces.
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="V1 expects a Health Auto Export JSON payload (CSV support is a follow-up).",
        )

    import json as _json
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Auto Export JSON parse failed: {e}",
        ) from e

    parsed = auto_export_ingest.parse_health_auto_export(payload)

    async def _stream():
        yield raw

    blob = await storage.write_blob(_stream(), suffix=".json")

    src_id = uuid.uuid4()
    src = SourceDocument(
        id=src_id,
        owner_user_id=user.id,
        source_type="auto_export",
        original_filename=file.filename,
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type="application/json",
        acquired_at=datetime.now(timezone.utc),
        source_system="health_auto_export",
        source_label=source_label or (file.filename or "Auto Export"),
        raw_metadata={
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
            "metric_counts": parsed.metric_counts,
            "workout_count": parsed.workout_count,
            "sleep_session_count": parsed.sleep_session_count,
            "fact_count": len(parsed.facts),
            "skipped_metrics": sorted(set(parsed.skipped_metrics)),
            "parse_warnings": parsed.parse_warnings[:50],
        },
    )
    db.add(src)
    await db.flush()

    # One anchor per fact, anchored back to the same SourceDocument.
    # We use anchor_type='auto_export_metric' so future timeline
    # rendering can distinguish wearable evidence from clinical.
    for f in parsed.facts:
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            anchor_type="auto_export_metric",
            section_path=";".join(
                f"{k}={','.join(v)}" for k, v in (f.coded_concepts or {}).items()
            ) or None,
            text_excerpt=f.label,
        )
        db.add(anchor)
        await db.flush()
        db.add(
            ExtractedFact(
                fact_type=f.fact_type,
                label=f.label,
                description=f.description,
                date_start=f.date_start,
                date_end=f.date_end,
                date_precision="day",
                coded_concepts=f.coded_concepts or None,
                confidence=f.confidence,
                review_state="confirmed",
                evidence_anchor_ids=[anchor.id],
                extraction_method="health_auto_export",
            )
        )

    await db.commit()
    await db.refresh(src)
    log.info(
        "auto_export_ingested",
        source_id=str(src.id),
        fact_count=len(parsed.facts),
        workouts=parsed.workout_count,
        sleep_sessions=parsed.sleep_session_count,
        skipped_metrics=len(set(parsed.skipped_metrics)),
    )
    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=False,
    )


class ExtractFactsRequest(BaseModel):
    only_pages: list[int] | None = None
    patient_context: str | None = None


class ExtractionJobReadout(BaseModel):
    job_id: str
    status: str  # pending | running | completed | failed | cancelled
    total_pages: int
    completed_pages: int
    facts_added: int
    page_errors: list[dict]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


def _job_readout(j: ExtractionJob) -> ExtractionJobReadout:
    return ExtractionJobReadout(
        job_id=str(j.id),
        status=j.status,
        total_pages=j.total_pages,
        completed_pages=j.completed_pages,
        facts_added=j.facts_added,
        page_errors=list(j.page_errors or []),
        error=j.error,
        created_at=j.created_at,
        started_at=j.started_at,
        completed_at=j.completed_at,
    )


class PatchSourceBody(BaseModel):
    event_date: datetime | None = None
    caption: str | None = None
    source_label: str | None = None


@router.patch("/{source_id}", response_model=SourceDetail)
async def patch_source(
    source_id: uuid.UUID,
    body: PatchSourceBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceDetail:
    """Mutate user-supplied fields on a SourceDocument.

    Currently scoped to event_date / caption / source_label. The
    primary use case is the "No date on this upload" hint on the
    source detail page — user sets an event date after the fact so
    the upload can participate in timeline / dossier retrieval.

    Setting event_date also re-runs attach_nearby_clinical_events so
    the "Same window in your record" panel populates with whatever
    major facts lived in the now-known window.
    """
    src = await db.get(SourceDocument, source_id)
    if src is None or src.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    touched = False
    if body.event_date is not None:
        src.user_supplied_event_date = body.event_date
        # captured_at is for EXIF-derived dates; user-supplied dates
        # live in user_supplied_event_date. Both feed the upload
        # context anchor.
        touched = True
    if body.caption is not None:
        src.user_supplied_caption = body.caption.strip() or None
        touched = True
    if body.source_label is not None:
        src.source_label = body.source_label.strip() or None
        touched = True
    if touched and src.source_type in {"photo", "note", "voice_memo"}:
        await attach_nearby_clinical_events(db, user, src)
    await db.commit()
    await db.refresh(src)
    return SourceDetail(
        **_to_summary(src).model_dump(),
        storage_uri=src.storage_uri,
        hash=src.hash,
        mime_type=src.mime_type,
        acquired_at=src.acquired_at,
        raw_metadata=src.raw_metadata,
        exif_metadata=src.exif_metadata,
        has_gps=bool(src.exif_metadata and src.exif_metadata.get("gps")),
    )


@router.post("/{source_id}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def trigger_photo_analyze(
    source_id: uuid.UUID,
    force: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Explicit "Analyze these" trigger for a personal photo upload.

    Companion to the batch_import=true upload mode: bulk camera-roll
    imports land without auto-vision; this endpoint lets the user
    cherry-pick which ones to actually analyze. Idempotent by default
    — if raw_metadata.vision is already populated, returns
    `already_analyzed` without re-running so we don't double-charge
    on content that hasn't changed.

    Pass `?force=true` to re-run vision on a photo that has already
    been analyzed. Use case: the personal-photo prompt has changed
    materially (e.g. 2026-05-16's structured_facts addition) and
    older photos need to be re-extracted to pick up the new fields.
    """
    src = await db.get(SourceDocument, source_id)
    if src is None or src.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if src.source_type != "photo":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"/analyze only supports photo sources, got {src.source_type}",
        )
    if not force and (src.raw_metadata or {}).get("vision") is not None:
        return {"status": "already_analyzed"}

    job_id = await enqueue_personal_photo_vision(str(src.id))
    # Mark pending so the UI can spin until the worker completes.
    raw = dict(src.raw_metadata or {})
    raw["vision_pending"] = True
    src.raw_metadata = raw
    await db.commit()
    return {"status": "enqueued", "job_id": job_id, "forced": str(force).lower()}


@router.post("/{source_id}/extract-facts", status_code=status.HTTP_202_ACCEPTED)
async def extract_facts_from_source(
    source_id: uuid.UUID,
    body: ExtractFactsRequest = ExtractFactsRequest(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ExtractionJobReadout:
    """Enqueue a background vision-extraction job and return immediately.

    The actual page-by-page Anthropic calls run in the Arq worker. The
    UI polls `GET /api/sources/{source_id}/extraction-status` for
    progress (one notification per page completed). A partial unique
    DB index prevents two concurrent jobs against the same source, so
    a re-click while one is running returns the existing job.
    """
    require_phi_consent(user)
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if src.source_type not in {"pdf", "fax_pdf"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Vision extraction supports pdf/fax_pdf sources, got {src.source_type}",
        )

    # If an in-flight job already exists, return it instead of erroring.
    in_flight = (await db.execute(
        select(ExtractionJob)
        .where(ExtractionJob.source_document_id == source_id)
        .where(ExtractionJob.status.in_(("pending", "running")))
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if in_flight is not None:
        return _job_readout(in_flight)

    job = ExtractionJob(
        source_document_id=source_id,
        user_id=user.id,
        status="pending",
        only_pages=body.only_pages,
        patient_context=body.patient_context,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    arq_job_id = await enqueue_extraction_job(str(job.id))
    if arq_job_id:
        job.arq_job_id = arq_job_id
        await db.commit()

    log.info("vision_job_enqueued", job_id=str(job.id), source_id=str(source_id))
    return _job_readout(job)


@router.get("/{source_id}/extraction-status")
async def get_extraction_status(
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ExtractionJobReadout | None:
    """Latest extraction job for this source, or null if none yet.

    Polled by the UI every few seconds while a job is running.
    """
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    job = (await db.execute(
        select(ExtractionJob)
        .where(ExtractionJob.source_document_id == source_id)
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if job is None:
        return None
    return _job_readout(job)


class AnchorReadout(BaseModel):
    id: str
    anchor_type: str
    page_number: int | None
    section_path: str | None
    text_excerpt: str | None


@router.get("/{source_id}/anchors")
async def list_anchors(
    source_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[AnchorReadout]:
    """Return every evidence anchor on this source.

    Used by the source detail page to surface the supporting text
    excerpt next to each rendered page (CAIHL "why do you think
    that?" — the user sees the actual quote that grounds each
    extracted fact, not just the page image).
    """
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    rows = (await db.execute(
        select(EvidenceAnchor)
        .where(EvidenceAnchor.source_document_id == source_id)
        .order_by(
            EvidenceAnchor.page_number.asc().nullslast(),
            EvidenceAnchor.created_at.asc(),
        )
    )).scalars().all()
    return [
        AnchorReadout(
            id=str(a.id),
            anchor_type=a.anchor_type,
            page_number=a.page_number,
            section_path=a.section_path,
            text_excerpt=a.text_excerpt,
        )
        for a in rows
    ]


@router.get("/{source_id}/page/{page_number}")
async def get_page_image(
    source_id: uuid.UUID,
    page_number: int,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    page_renders = (src.raw_metadata or {}).get("page_renders", []) or []
    match = next((p for p in page_renders if int(p.get("page", 0)) == page_number), None)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    p = Path(str(match.get("image_path", "")))
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page image missing on disk")
    return FileResponse(p, media_type="image/png")


@router.get("/{source_id}/thumb/{size}")
async def get_thumbnail(
    source_id: uuid.UUID,
    size: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    if size not in images.THUMB_SIZES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid size")
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    thumbs = (src.raw_metadata or {}).get("thumbnails", {})
    path_str = thumbs.get(size)
    if not path_str:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not generated")
    p = Path(path_str)
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail file missing")
    return FileResponse(p, media_type="image/webp")


# ---------------------------------------------------------------------------
# Contribution summary per source (docs/07 R2 — patient-meaningful lead)
# ---------------------------------------------------------------------------


class SourceDossierLinkage(BaseModel):
    slug: str
    name: str
    fact_count: int


class SourceTopEvent(BaseModel):
    id: str
    fact_type: str
    label: str
    display_label: str | None = None
    date_start: datetime | None
    review_state: str


class SourceContributionSummary(BaseModel):
    """Patient-meaningful narrative for a single source.

    Replaces the file-inspector lead ("filename / MIME / SHA-256 /
    424 facts") with "what this source actually contributed to your
    record": volume, time span, connected dossiers, the most-named
    care event if one exists, and what still needs review.
    """

    source_id: str
    source_name: str
    summary: str            # one-paragraph patient-readable
    total_facts: int
    needs_review_count: int
    fact_type_counts: dict[str, int]
    date_min: datetime | None
    date_max: datetime | None
    top_events: list[SourceTopEvent]
    dossier_linkages: list[SourceDossierLinkage]


# Same FHIR-resource-ID pattern as Notable moments / narrative —
# exclude legacy garbage labels from "top events."
_FHIR_ID_LABEL_RE = re.compile(
    r"^(Encounter|MedicationRequest|MedicationDispense|MedicationStatement|"
    r"Procedure|Condition|Observation|DiagnosticReport|AllergyIntolerance|"
    r"Immunization|Resource) [A-Za-z0-9._\-]{12,}$"
)


def _fact_matches_topic(fact: ExtractedFact, topic) -> bool:
    """Replicate `topic_membership_clause` semantics in Python so we
    can score this source's already-fetched facts against every topic
    without running N SQL queries. Substring on aliases + name;
    case-insensitive regex on `label_patterns`."""
    label = (fact.label or "").lower()
    desc = (fact.description or "").lower()
    for alias in (topic.name, *(topic.aliases or [])):
        if not alias:
            continue
        a = alias.lower()
        if a in label or a in desc:
            return True
    for pat in (topic.label_patterns or []):
        if not pat:
            continue
        try:
            if re.search(pat, fact.label or "", re.IGNORECASE):
                return True
            if re.search(pat, fact.description or "", re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _build_source_narrative(
    src: SourceDocument,
    *,
    total_facts: int,
    needs_review_count: int,
    fact_type_counts: dict[str, int],
    date_min: datetime | None,
    date_max: datetime | None,
    top_events: list[ExtractedFact],
    linkages: list[dict],
) -> str:
    """One-paragraph narrative. Patient-readable, never the resource
    ID. Care-meaningful events lead when present; otherwise the
    narrative names the import volume + scope honestly."""
    name = (src.source_label or src.original_filename or "Untitled source").strip()

    # Time framing
    if date_min and date_max:
        if date_min.year == date_max.year:
            span_clause = f"events from {date_min.year}"
        else:
            span_clause = f"events from {date_min.year}–{date_max.year}"
    elif date_min:
        span_clause = f"events from {date_min.year}"
    else:
        span_clause = "no dated events"

    parts: list[str] = []
    parts.append(
        f"**{name}**, ingested "
        f"{src.acquired_at.date().isoformat()}."
    )
    parts.append(
        f"This source added {total_facts:,} fact{'' if total_facts == 1 else 's'} "
        f"to your record, with {span_clause}."
    )

    # Care-meaningful anchor — only when there's a real labeled event.
    anchor = None
    for ev in top_events:
        if ev.date_start is not None:
            anchor = ev
            break
    if anchor is not None:
        date_str = anchor.date_start.date().isoformat()
        parts.append(
            f"Anchors around {anchor.label} on {date_str}."
        )

    # Dossier linkages
    if linkages:
        if len(linkages) == 1:
            parts.append(f"Connected into the {linkages[0]['name']} dossier.")
        else:
            names = ", ".join(l["name"] for l in linkages[:4])
            parts.append(
                f"Connected into {len(linkages)} dossier"
                f"{'' if len(linkages) == 1 else 's'}: {names}."
            )

    # Review-still-needed reminder
    if needs_review_count > 0:
        parts.append(
            f"{needs_review_count:,} fact"
            f"{'' if needs_review_count == 1 else 's'} need your review."
        )

    return " ".join(parts)


@router.get("/{source_id}/contribution-summary")
async def get_source_contribution_summary(
    source_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceContributionSummary:
    """Patient-meaningful narrative + structured contribution data
    for a single source. Powers the source detail page lead (R2 of
    the 2026-05-10 product reframe)."""
    src = await db.get(SourceDocument, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Resolve all anchors for this source, then all facts.
    anchor_ids = list((await db.execute(
        select(EvidenceAnchor.id).where(EvidenceAnchor.source_document_id == source_id)
    )).scalars().all())
    if not anchor_ids:
        return SourceContributionSummary(
            source_id=str(source_id),
            source_name=(src.source_label or src.original_filename or "Untitled source"),
            summary=(
                f"{src.source_label or src.original_filename or 'Untitled source'}, "
                f"ingested {src.acquired_at.date().isoformat()}. "
                "This source hasn't produced any extracted facts yet."
            ),
            total_facts=0,
            needs_review_count=0,
            fact_type_counts={},
            date_min=None,
            date_max=None,
            top_events=[],
            dossier_linkages=[],
        )

    facts = list((await db.execute(
        select(ExtractedFact).where(
            ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids)
        )
    )).scalars().all())

    total_facts = len(facts)
    fact_type_counts: dict[str, int] = {}
    needs_review_count = 0
    dated: list[ExtractedFact] = []
    for f in facts:
        fact_type_counts[f.fact_type] = fact_type_counts.get(f.fact_type, 0) + 1
        if f.review_state == "needs_review":
            needs_review_count += 1
        if f.date_start is not None:
            dated.append(f)

    date_min = min((f.date_start for f in dated), default=None)
    date_max = max((f.date_start for f in dated), default=None)

    # Top events: care-meaningful types (procedure / condition /
    # encounter), labeled (not a FHIR resource ID), most recent first.
    candidate_events = [
        f for f in facts
        if f.fact_type in {"procedure", "condition", "encounter"}
        and not _FHIR_ID_LABEL_RE.match(f.label or "")
        and f.review_state not in ("deferred", "rejected", "source_only")
    ]
    candidate_events.sort(
        key=lambda f: (
            f.date_start.timestamp() if f.date_start else 0,
            f.confidence or 0,
        ),
        reverse=True,
    )
    top_events_raw = candidate_events[:5]
    top_events_out = [
        SourceTopEvent(
            id=str(f.id),
            fact_type=f.fact_type,
            label=f.label,
            display_label=f.display_label,
            date_start=f.date_start,
            review_state=f.review_state,
        )
        for f in top_events_raw
    ]

    # Dossier linkages — N topics, each scored against the
    # already-fetched facts. Cheaper than re-fetching for each topic
    # via SQL; typical install has < 20 topics.
    from ..models.topic import Topic
    topics = list((await db.execute(select(Topic))).scalars().all())
    linkages: list[dict] = []
    for t in topics:
        matched = sum(1 for f in facts if _fact_matches_topic(f, t))
        if matched > 0:
            linkages.append({"slug": t.slug, "name": t.name, "fact_count": matched})
    linkages.sort(key=lambda x: -x["fact_count"])
    linkages_out = [
        SourceDossierLinkage(
            slug=l["slug"], name=l["name"], fact_count=l["fact_count"]
        )
        for l in linkages
    ]

    summary = _build_source_narrative(
        src,
        total_facts=total_facts,
        needs_review_count=needs_review_count,
        fact_type_counts=fact_type_counts,
        date_min=date_min,
        date_max=date_max,
        top_events=top_events_raw,
        linkages=linkages,
    )

    return SourceContributionSummary(
        source_id=str(source_id),
        source_name=(src.source_label or src.original_filename or "Untitled source"),
        summary=summary,
        total_facts=total_facts,
        needs_review_count=needs_review_count,
        fact_type_counts=fact_type_counts,
        date_min=date_min,
        date_max=date_max,
        top_events=top_events_out,
        dossier_linkages=linkages_out,
    )


# ---------------------------------------------------------------------------
# Review summary per source (docs/07 Priority 1 §453-468)
# ---------------------------------------------------------------------------


class SourceReviewSummary(BaseModel):
    """Aggregate of a source's review backlog so the user can clear a
    whole CCDA/fax in one decision instead of N small ones.

    Counts segment the source's facts into three buckets:
      - timeline_relevant: confirmed or needs_review on substantive
        fact_types (procedure, condition, medication, encounter,
        symptom, observation, lab_result, imaging_study,
        life_context_event, inferred_relationship)
      - provider_contact: provider_relationship facts (the noise lane)
      - already_resolved: rejected / deferred / source_only / corrected

    The detail page renders a one-paragraph callout with these counts
    and a small action row: review timeline-relevant, defer all
    provider-contact, keep provider-contact as source-only, open the
    full fact table.
    """

    source_id: str
    total_facts: int
    needs_review_count: int
    timeline_relevant_needs_review: int
    provider_contact_needs_review: int
    confirmed_count: int
    deferred_or_resolved_count: int
    by_fact_type: dict[str, int]


_PROVIDER_FACT_TYPES = ("provider_relationship",)
_RESOLVED_STATES = ("rejected", "deferred", "source_only", "corrected")


@router.get("/{source_id}/review-summary")
async def get_source_review_summary(
    source_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SourceReviewSummary:
    """One-shot summary of this source's review backlog. Powers the
    Source detail page's review callout — "412 extracted facts, 37
    timeline-relevant, 9 may duplicate, 344 are provider/contact
    details" (docs/07 §458-464)."""
    # Resolve anchors for the source so we can find its facts.
    anchor_ids = list((await db.execute(
        select(EvidenceAnchor.id).where(EvidenceAnchor.source_document_id == source_id)
    )).scalars().all())
    if not anchor_ids:
        return SourceReviewSummary(
            source_id=str(source_id),
            total_facts=0,
            needs_review_count=0,
            timeline_relevant_needs_review=0,
            provider_contact_needs_review=0,
            confirmed_count=0,
            deferred_or_resolved_count=0,
            by_fact_type={},
        )

    rows = (await db.execute(
        select(
            ExtractedFact.fact_type,
            ExtractedFact.review_state,
            func.count().label("n"),
        )
        .where(ExtractedFact.evidence_anchor_ids.op("&&")(anchor_ids))
        .group_by(ExtractedFact.fact_type, ExtractedFact.review_state)
    )).all()

    total = 0
    needs_review = 0
    timeline_relevant_review = 0
    provider_review = 0
    confirmed = 0
    resolved = 0
    by_fact_type: dict[str, int] = {}
    for fact_type, state, n in rows:
        n = int(n)
        total += n
        by_fact_type[fact_type] = by_fact_type.get(fact_type, 0) + n
        if state == "needs_review":
            needs_review += n
            if fact_type in _PROVIDER_FACT_TYPES:
                provider_review += n
            else:
                timeline_relevant_review += n
        elif state in ("confirmed", "auto_confirmed"):
            confirmed += n
        elif state in _RESOLVED_STATES:
            resolved += n
        else:
            # 'new' or anything unforeseen — count as needs_review for
            # the UI summary so it doesn't silently disappear.
            needs_review += n
            timeline_relevant_review += n

    return SourceReviewSummary(
        source_id=str(source_id),
        total_facts=total,
        needs_review_count=needs_review,
        timeline_relevant_needs_review=timeline_relevant_review,
        provider_contact_needs_review=provider_review,
        confirmed_count=confirmed,
        deferred_or_resolved_count=resolved,
        by_fact_type=by_fact_type,
    )
