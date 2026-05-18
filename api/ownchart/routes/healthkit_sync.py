"""Native HealthKit sync (PR2).

Endpoints:
  GET  /api/healthkit/capabilities  — registry the iOS app keys off
  POST /api/healthkit/sync          — one identifier per request, batched
  GET  /api/healthkit/sync/cursors  — resume-after-reinstall anchors
  POST /api/healthkit/sync/deletions — tombstone HK-deleted samples (V1: stub)

Wire format is documented in docs/BACKEND_PR1_PR2_SPEC.md. The
constraints layered on top:

  - **No raw high-volume facts by default**: server rejects raw posts
    for HR / SpO2 / steps / energy / etc. via `enforce_strategy()`.
  - **Source-neutral dedupe**: client_sample_key is `agg-<id>-<date>`
    for aggregates (date-keyed, naturally source-neutral) or
    `sha256(identifier|start|end|value)` for raw (content-derived,
    so the same reading from Apple Watch + iPhone collapses).
  - **Demo mode**: `mode="demo"` caps batches at 500 and refuses raw
    posts for heart/activity scopes. The iOS app's alpha defaults to
    demo. Production sync turns on `mode="full"` later.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from ..canonical.equivalence import daily_metric_key
from ..core.auth_context import AuthContext, get_auth_context, require_role
from ..core.config import get_settings
from ..core.db import get_session
from ..core.device_auth import get_user_from_device_token_or_session
from ..core.logger import get_logger
from ..ingest.healthkit import (
    HK_REGISTRY,
    StrategyRejected,
    enforce_strategy,
    format_aggregate_label,
    format_raw_label,
    registry_for_capabilities,
)
from ..ingest.healthkit_workout import (
    HKDevice,
    HKSource,
    HKWorkoutSample,
    SyncMode,
    workout_sample_to_fact_shape,
)
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.healthkit_cursor import HealthKitCursor
from ..models.source_document import SourceDocument
from ..models.user import User

router = APIRouter()
log = get_logger("ownchart.routes.healthkit_sync")

# Hard cap per batch — the iOS client must chunk beyond this. Demo
# mode tightens this further (see DEMO_BATCH_CAP).
BATCH_CAP = 5000
DEMO_BATCH_CAP = 500


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class CapabilitiesResponse(BaseModel):
    server_version: str
    identifiers: list[dict]


@router.get("/capabilities")
async def get_capabilities(
    _user: User = Depends(get_user_from_device_token_or_session),
) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        server_version="0.1.0",
        identifiers=registry_for_capabilities(),
    )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class SyncSample(BaseModel):
    client_sample_key: str = Field(..., max_length=128)
    start_at: datetime
    end_at: datetime
    value: float | None = None
    # iOS sends `display_text` for raw samples that aren't a simple
    # number (workouts, medications, symptoms). Aggregates leave it
    # empty.
    display_text: str | None = None
    # Optional source metadata (Apple Watch vs iPhone vs Withings, etc.)
    # Stored on the fact for traceability; NOT used for dedupe.
    source_name: str | None = None
    source_bundle_id: str | None = None
    # iOS may send its HKObject UUID for raw samples. Stored in
    # coded_concepts.hk_uuid; not the dedupe key.
    hk_uuid: str | None = None

    # ── HKWorkoutType fields (M02 Slice 2, BE-3 contract) ──
    # iOS populates these only when identifier == "HKWorkoutType".
    # For other identifiers they stay None. Validation of the
    # workout-required subset happens inside the workout branch of
    # _sync_healthkit_inner; an HKWorkoutType sample missing any
    # required workout field surfaces as a 422 from
    # _build_workout_fact_payload via HKWorkoutSample's own
    # pydantic checks.
    workout_activity_type: str | None = Field(default=None, max_length=64)
    workout_activity_type_raw: int | None = None
    duration_s: float | None = Field(default=None, ge=0)
    distance_m: float | None = Field(default=None, ge=0)
    energy_kcal: float | None = Field(default=None, ge=0)
    source: HKSource | None = None  # nested source for workout samples
    device: HKDevice | None = None  # optional device (Apple Watch, etc.)
    metadata: dict[str, Any] | None = None  # sample-level metadata


class SyncRequest(BaseModel):
    device_id: str
    identifier: str
    strategy: Literal["daily_aggregate", "raw"]
    unit: str | None = None
    # Advisory only. Server overrides this from settings.demo_mode at
    # request time — clients no longer need to mirror an instance flag.
    # Left in the schema so old iOS builds don't 422 on an unknown field.
    mode: Literal["demo", "full"] = "full"
    # M02 Slice 2: iOS sends "backfill" while iterating historical
    # samples on initial discovery, "incremental" once anchored on
    # the watch's current cutoff. Recorded as observational metadata
    # on workout facts via raw_metadata.healthkit.sync_mode; the
    # BE-3 invariant is that storage is otherwise IDENTICAL across
    # the two modes. Default "incremental" keeps pre-Slice-2 iOS
    # builds working without sending the new field.
    sync_mode: SyncMode = "incremental"
    samples: list[SyncSample]
    anchor_blob: str | None = None  # base64-encoded HKQueryAnchor.archivedData


class SyncResponse(BaseModel):
    accepted: int
    deduplicated: int
    cursor_id: str
    anchor_blob: str | None
    mode: str


def _decode_anchor(blob: str | None) -> bytes | None:
    if not blob:
        return None
    try:
        return base64.b64decode(blob)
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="anchor_blob is not valid base64",
        )


def _encode_anchor(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return base64.b64encode(blob).decode("ascii")


async def _upsert_source_for_day(
    db: AsyncSession,
    user: User,
    device_token_id: uuid.UUID | None,
    day: datetime,
    mode: str,
    *,
    person_record_id: uuid.UUID,
) -> SourceDocument:
    """One SourceDocument per (user, person_record, device_token, day) —
    facts hang off it via evidence anchors. Matches the auto_export
    pattern.

    M02 perimeter (Batch 8): the source is record-scoped, so the
    SELECT-existing query MUST filter by person_record_id too — a
    caregiver who pairs both Mom's and Dad's iPhone must NOT
    collapse their day-sources into a single row. The dedupe lane
    is `(person_record, user, source_type, source_label)`.

    Race-safe: iOS uploads multiple identifiers in parallel via URLSession
    with no client-side serialization, so two pages may both see "no row"
    and both try to insert. We try-insert-then-select-fallback: if our
    insert fails because another concurrent batch already created the
    same day-source, swallow the IntegrityError and re-select.
    """
    day_start = day.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    label = f"native-healthkit-{day_start.date().isoformat()}"

    def _select_existing():
        # ORDER BY id keeps the choice deterministic across calls so any
        # downstream caches that key on source_document_id stay stable.
        # .scalars().first() — not scalar_one_or_none — because pre-fix
        # parallel uploads created duplicate rows (no unique constraint
        # on source_documents); a 0024 migration dedupes them, but we
        # stay tolerant in case a backfill ever re-introduces them.
        return (
            select(SourceDocument)
            .where(SourceDocument.owner_user_id == user.id)
            .where(SourceDocument.person_record_id == person_record_id)
            .where(SourceDocument.source_type == "native_healthkit")
            .where(SourceDocument.source_label == label)
            .order_by(SourceDocument.id.asc())
        )

    existing = (await db.execute(_select_existing())).scalars().first()
    if existing is not None:
        return existing
    # Try-insert inside a savepoint so an IntegrityError from a concurrent
    # winner doesn't poison the outer transaction. If insertion fails, the
    # savepoint rolls back to just before db.add(), and we re-select.
    try:
        async with db.begin_nested():
            src = SourceDocument(
                id=uuid.uuid4(),
                owner_user_id=user.id,
                person_record_id=person_record_id,
                source_type="native_healthkit",
                original_filename=f"{label}.batch",
                storage_uri="memory://native-healthkit",
                hash=f"native-healthkit-{day_start.isoformat()}",
                mime_type="application/json",
                acquired_at=datetime.now(timezone.utc),
                source_system="HealthKit",
                source_label=label,
                raw_metadata={
                    "device_token_id": str(device_token_id) if device_token_id else None,
                    "day": day_start.date().isoformat(),
                    "demo": mode == "demo",
                },
            )
            db.add(src)
            await db.flush()
        return src
    except Exception:
        existing = (await db.execute(_select_existing())).scalars().first()
        if existing is None:
            raise  # not a race, surface the real failure
        return existing


@router.post("/sync")
async def sync_healthkit(
    body: SyncRequest,
    request: Request,
    ctx: AuthContext = Depends(require_role("caregiver")),
    db: AsyncSession = Depends(get_session),
) -> SyncResponse:
    """Ingest one HK-identifier batch.

    M02 perimeter (Batch 8): HealthKit sync writes records that are
    record-shaping (Source + Facts on a person_record). The iOS app
    sends `X-OwnChart-Person-Record` to pin which record the device
    is currently paired with; `get_auth_context` (inside
    `require_role`) resolves it. Caregiver+ required since this is
    a write that creates new sources/facts.
    """
    user = ctx.user
    try:
        return await _sync_healthkit_inner(body, request, user, ctx, db)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Match the commit 13df6d8 pattern: log full traceback at warn so
        # iOS dev can tail logs and triage; surface exception type+message
        # in the 500 detail only when env=dev or debug_payloads, never on
        # prod (could leak PHI substrings).
        log.warning(
            "healthkit_sync_failed",
            user_id=str(user.id),
            person_record_id=str(ctx.active_record_id),
            identifier=body.identifier,
            strategy=body.strategy,
            sample_count=len(body.samples),
            exc_type=type(exc).__name__,
            exc=str(exc),
            exc_info=True,
        )
        s = get_settings()
        if s.env == "dev" or s.debug_payloads:
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = "healthkit sync failed; see server logs"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        )


def _format_workout_label(
    coded: dict[str, Any], raw_meta: dict[str, Any],
) -> str:
    """Human-readable label for a workout fact.

    Format: "<Activity> — <duration> min[, <distance> km][, <energy> kcal]".
    Examples:
      - "Running — 36 min, 8.0 km, 615 kcal"
      - "Strength training — 25 min"

    Used by both the live route and the test harness. Pure function:
    same (coded, raw_meta) → same label.
    """
    activity_raw = coded.get("workout_activity_type") or "workout"
    activity = activity_raw.replace("_", " ")
    activity = activity[:1].upper() + activity[1:]
    parts: list[str] = []
    duration_s = raw_meta.get("duration_s")
    if duration_s is not None and duration_s > 0:
        parts.append(f"{round(duration_s / 60)} min")
    distance_m = raw_meta.get("distance_m")
    if distance_m:
        parts.append(f"{distance_m / 1000:.1f} km")
    energy_kcal = raw_meta.get("energy_kcal")
    if energy_kcal:
        parts.append(f"{round(energy_kcal)} kcal")
    if not parts:
        return activity
    return f"{activity} — " + ", ".join(parts)


def _build_workout_fact_payload(
    sample: SyncSample, *, sync_mode: SyncMode,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Map an HKWorkoutType ``SyncSample`` to fact-insert parameters.

    Returns ``(label, coded_concepts, raw_metadata_healthkit)``. The
    last value is the dict to store under ``raw_metadata.healthkit``
    (the route wraps it in ``{"healthkit": ...}`` before persisting).

    Validates the workout-required subset of fields explicitly and
    raises ``HTTPException(422)`` if any are missing, with a message
    naming the bad ``client_sample_key`` so the iOS side can find the
    offending sample in its outbox. Then delegates to the BE-3
    contract's ``workout_sample_to_fact_shape`` so the storage shape
    stays a single source of truth (commit ``077a10a``).

    Pure: same input → same output. ``sync_mode`` lands ONLY in
    ``raw_metadata_healthkit['sync_mode']``; everything else is
    identical between ``"backfill"`` and ``"incremental"`` calls —
    BE-3's mode-agnostic invariant carried into the live ingest path.
    """
    missing: list[str] = []
    if not sample.workout_activity_type:
        missing.append("workout_activity_type")
    if sample.duration_s is None:
        missing.append("duration_s")
    if sample.source is None or not sample.source.name:
        missing.append("source.name")
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "HKWorkoutType sample "
                f"{sample.client_sample_key!r} is missing required "
                f"workout fields: {', '.join(missing)}"
            ),
        )
    try:
        hk = HKWorkoutSample(
            client_sample_key=sample.client_sample_key,
            start_at=sample.start_at.isoformat(),
            end_at=sample.end_at.isoformat(),
            workout_activity_type=sample.workout_activity_type,  # type: ignore[arg-type]
            workout_activity_type_raw=sample.workout_activity_type_raw,
            duration_s=sample.duration_s,  # type: ignore[arg-type]
            distance_m=sample.distance_m,
            energy_kcal=sample.energy_kcal,
            source=sample.source,  # type: ignore[arg-type]
            device=sample.device,
            metadata=sample.metadata,
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "HKWorkoutType sample "
                f"{sample.client_sample_key!r} failed BE-3 validation: "
                f"{e.errors()[0].get('msg', str(e))}"
            ),
        )
    coded, raw_meta = workout_sample_to_fact_shape(hk, sync_mode=sync_mode)
    label = _format_workout_label(coded, raw_meta)
    return label, coded, raw_meta


async def _sync_healthkit_inner(
    body: SyncRequest,
    request: Request,
    user: User,
    ctx: AuthContext,
    db: AsyncSession,
) -> SyncResponse:
    if body.identifier not in HK_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown identifier {body.identifier}",
        )

    # Single source of truth for demo gating: the instance's
    # settings.demo_mode flag. body.mode used to be the lever, but iOS
    # clients had no reason to know about an instance setting and the
    # field defaulted to "demo" — which silently 422'd every raw post on
    # real-instance deployments. Server now overrides body.mode entirely.
    s = get_settings()
    effective_mode: Literal["demo", "full"] = "demo" if s.demo_mode else "full"

    cap = DEMO_BATCH_CAP if effective_mode == "demo" else BATCH_CAP
    if len(body.samples) > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Batch too large (>{cap}); chunk and retry",
        )

    try:
        spec = enforce_strategy(body.identifier, body.strategy, effective_mode)
    except StrategyRejected as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    device_token_id: uuid.UUID | None = getattr(
        request.state, "device_token_id", None
    )
    if device_token_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="/sync requires a device bearer token, not a session cookie",
        )

    # Upsert the per-device cursor for this identifier. iOS may parallel-
    # POST multiple pages of the same identifier; the
    # uq_hkcursor_user_dev_id unique constraint would IntegrityError on the
    # losing page if we did a check-then-insert. Use ON CONFLICT DO NOTHING
    # + re-select to be race-safe.
    cursor = (await db.execute(
        select(HealthKitCursor).where(
            HealthKitCursor.user_id == user.id,
            HealthKitCursor.device_token_id == device_token_id,
            HealthKitCursor.identifier == body.identifier,
        )
    )).scalar_one_or_none()
    if cursor is None:
        await db.execute(
            pg_insert(HealthKitCursor.__table__).values(
                id=uuid.uuid4(),
                user_id=user.id,
                device_token_id=device_token_id,
                identifier=body.identifier,
                anchor_blob=_decode_anchor(body.anchor_blob),
                last_sample_end_at=None,
                last_strategy=body.strategy,
                sample_count=0,
            ).on_conflict_do_nothing(
                constraint="uq_hkcursor_user_dev_id",
            )
        )
        # Re-select — whether we won or lost the race, the row exists now.
        cursor = (await db.execute(
            select(HealthKitCursor).where(
                HealthKitCursor.user_id == user.id,
                HealthKitCursor.device_token_id == device_token_id,
                HealthKitCursor.identifier == body.identifier,
            )
        )).scalar_one()
        cursor.anchor_blob = _decode_anchor(body.anchor_blob)
        cursor.last_strategy = body.strategy
    else:
        cursor.anchor_blob = _decode_anchor(body.anchor_blob)
        cursor.last_strategy = body.strategy

    accepted = 0
    deduped = 0

    # Group samples by UTC day so we can pin them to the right
    # SourceDocument. Most batches will be one day or close to it.
    samples_by_day: dict[str, list[SyncSample]] = {}
    for s in body.samples:
        day_key = s.start_at.astimezone(timezone.utc).date().isoformat()
        samples_by_day.setdefault(day_key, []).append(s)

    source_doc_by_day: dict[str, SourceDocument] = {}
    for day_key, day_samples in samples_by_day.items():
        # All samples in this day share one SourceDocument.
        first = day_samples[0]
        src = await _upsert_source_for_day(
            db, user, device_token_id, first.start_at, effective_mode,
            person_record_id=ctx.active_record_id,
        )
        source_doc_by_day[day_key] = src

        for s in day_samples:
            # M02 Slice 2: HKWorkoutType samples have their own
            # storage shape (BE-3 contract). Everything else goes
            # through the alpha-era path unchanged.
            if body.identifier == "HKWorkoutType":
                wlabel, wcoded, wraw = _build_workout_fact_payload(
                    s, sync_mode=body.sync_mode,
                )
                label = wlabel
                coded = wcoded
                method = "native_healthkit"
                equiv_key = None
                raw_metadata_value: dict | None = {"healthkit": wraw}
                date_precision_value: str | None = None
                fact_type_value = "observation"
                anchor_type_value = "healthkit_workout"
            else:
                label = (
                    format_aggregate_label(spec, s.value, body.unit)
                    if body.strategy == "daily_aggregate" and s.value is not None
                    else format_raw_label(spec, s.value, body.unit, s.display_text)
                )

                coded = {"hkquantitytype": [body.identifier]}
                if s.source_bundle_id:
                    coded["source_bundle_id"] = [s.source_bundle_id]
                if s.hk_uuid:
                    coded["hk_uuid"] = [s.hk_uuid]

                # Per-scope extraction_method routing:
                #   - clinical   → fhir_resource (Apple Health Records;
                #                  semantically the same shape as a FHIR
                #                  bundle import — lands in clinical lane)
                #   - medications / symptoms → patient_self_report (matches
                #                  Auto Export medication path; clinical lane)
                #   - everything else → native_healthkit (wearable lane)
                method = _extraction_method_for(spec)

                # Cross-source equivalence (docs/07 §487-545). For daily
                # aggregates we set a source-neutral key; same metric on
                # the same UTC date posted by Auto Export and native HK
                # collapses to one canonical event. NULL for raw samples
                # (workouts, sleep, body) — fuzzier dedupe rules in Phase 2.
                equiv_key = (
                    daily_metric_key(body.identifier, s.start_at)
                    if body.strategy == "daily_aggregate"
                    else None
                )

                raw_metadata_value = None
                date_precision_value = (
                    "day" if body.strategy == "daily_aggregate" else None
                )
                fact_type_value = _fact_type_for(spec)
                anchor_type_value = "healthkit_sample"

            # Idempotent insert via the partial unique index on
            # client_sample_key. ON CONFLICT DO NOTHING — replays
            # naturally dedupe. M02 perimeter: stamp the active
            # record id so retrieval scopes the wearable lane to
            # the right person.
            stmt = pg_insert(ExtractedFact.__table__).values(
                fact_type=fact_type_value,
                person_record_id=ctx.active_record_id,
                label=label[:512],
                description=None,
                date_start=s.start_at,
                date_end=s.end_at,
                date_precision=date_precision_value,
                coded_concepts=coded,
                raw_metadata=raw_metadata_value,
                confidence=95,
                review_state="confirmed",
                evidence_anchor_ids=[],
                extraction_method=method,
                client_sample_key=s.client_sample_key,
                equivalence_key=equiv_key,
            ).on_conflict_do_nothing(
                index_elements=["client_sample_key"],
                index_where=ExtractedFact.client_sample_key.isnot(None),
            ).returning(ExtractedFact.id)
            res = await db.execute(stmt)
            new_id = res.scalar_one_or_none()
            if new_id is not None:
                accepted += 1
                # Anchor the fact to the day's source so the timeline
                # / period drill can find it. We don't write a full
                # text_excerpt — aggregate samples have nothing
                # meaningful to quote.
                anchor = EvidenceAnchor(
                    source_document_id=src.id,
                    person_record_id=ctx.active_record_id,
                    anchor_type=anchor_type_value,
                    text_excerpt=label[:280],
                )
                db.add(anchor)
                await db.flush()
                # evidence_anchor_ids is a Postgres ARRAY; rewrite it
                # for the row we just inserted.
                await db.execute(
                    update(ExtractedFact)
                    .where(ExtractedFact.id == new_id)
                    .values(evidence_anchor_ids=[anchor.id])
                )
            else:
                deduped += 1

    cursor.sample_count += accepted
    # Newest sample's end_at — for the Devices page's "last synced"
    # display.
    newest = max((s.end_at for s in body.samples), default=None)
    if newest is not None and (
        cursor.last_sample_end_at is None or newest > cursor.last_sample_end_at
    ):
        cursor.last_sample_end_at = newest

    await db.commit()
    await db.refresh(cursor)

    log.info(
        "healthkit_sync_batch",
        user_id=str(user.id),
        device_token_id=str(device_token_id),
        identifier=body.identifier,
        strategy=body.strategy,
        mode=effective_mode,
        accepted=accepted,
        deduped=deduped,
    )
    return SyncResponse(
        accepted=accepted,
        deduplicated=deduped,
        cursor_id=str(cursor.id),
        anchor_blob=_encode_anchor(cursor.anchor_blob),
        mode=effective_mode,
    )


def _fact_type_for(spec) -> str:
    """Map HK scope (and a few specific identifiers) → fact_type.

    Most wearable / quantitative metrics land as `observation` so the
    timeline classifies them in the wearable lane (same as Auto
    Export). Medications and symptoms get their own fact_types so the
    /ask category-aware retrieval can find them. Health Records that
    Apple's Health.app exposes (allergies, conditions, immunizations,
    lab results, procedures) get semantic fact_types so they land in
    the clinical lane and feed the right dossiers.
    """
    # Scope-level overrides for explicit clinical fact types.
    if spec.scope in {"medications", "medication"}:
        return "medication"
    if spec.scope in {"symptoms", "symptom"}:
        return "symptom"
    # Per-identifier overrides for Health Records (clinical scope).
    if spec.scope == "clinical":
        return _CLINICAL_FACT_TYPES.get(spec.identifier, "observation")
    # Activity, heart, body, sleep, workouts, nutrition, mindfulness,
    # reproductive — all land as observations so they show up in the
    # wearable lane and existing cluster/period drill paths handle them.
    return "observation"


_CLINICAL_FACT_TYPES: dict[str, str] = {
    "HKClinicalTypeIdentifierAllergyRecord": "condition",
    "HKClinicalTypeIdentifierConditionRecord": "condition",
    "HKClinicalTypeIdentifierImmunizationRecord": "procedure",
    "HKClinicalTypeIdentifierLabResultRecord": "lab_result",
    "HKClinicalTypeIdentifierMedicationRecord": "medication",
    "HKClinicalTypeIdentifierProcedureRecord": "procedure",
    "HKClinicalTypeIdentifierVitalSignRecord": "observation",
}


def _extraction_method_for(spec) -> str:
    """Pick the extraction_method that lands the fact in the right
    timeline lane. Per docs/07 §680: native_healthkit is wearable;
    Health Records (clinical scope) flow through the clinical lane
    via the FHIR-resource path; user-attested medications/symptoms
    use patient_self_report (matches the Auto Export medication
    convention)."""
    if spec.scope == "clinical":
        return "fhir_resource"
    if spec.scope in {"medications", "medication", "symptoms", "symptom"}:
        return "patient_self_report"
    return "native_healthkit"


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


class CursorReadout(BaseModel):
    identifier: str
    anchor_blob: str | None
    last_sample_end_at: datetime | None
    last_strategy: str | None
    sample_count: int


class CursorsResponse(BaseModel):
    cursors: list[CursorReadout]


@router.get("/sync/cursors")
async def list_cursors(
    request: Request,
    user: User = Depends(get_user_from_device_token_or_session),
    db: AsyncSession = Depends(get_session),
) -> CursorsResponse:
    """Cursors for the calling device only. Resume backfill after a
    reinstall by replaying anchors per identifier."""
    device_token_id = getattr(request.state, "device_token_id", None)
    if device_token_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="/sync/cursors requires a device bearer token",
        )
    rows = (await db.execute(
        select(HealthKitCursor)
        .where(HealthKitCursor.user_id == user.id)
        .where(HealthKitCursor.device_token_id == device_token_id)
    )).scalars().all()
    return CursorsResponse(cursors=[
        CursorReadout(
            identifier=r.identifier,
            anchor_blob=_encode_anchor(r.anchor_blob),
            last_sample_end_at=r.last_sample_end_at,
            last_strategy=r.last_strategy,
            sample_count=r.sample_count,
        )
        for r in rows
    ])


# ---------------------------------------------------------------------------
# Deletions — V1 stub
# ---------------------------------------------------------------------------


class DeletionsRequest(BaseModel):
    identifier: str
    client_sample_keys: list[str]


class DeletionsResponse(BaseModel):
    tombstoned: int


@router.post("/sync/deletions")
async def post_deletions(
    body: DeletionsRequest,
    _user: User = Depends(get_user_from_device_token_or_session),
    db: AsyncSession = Depends(get_session),
) -> DeletionsResponse:
    """V1 stub: HealthKit deletion tombstoning is deferred (spec §
    "Deletions" — out of scope for alpha). Endpoint exists so the iOS
    client can POST without crashing; returns 0 tombstoned. Re-enable
    when we have a soft-delete strategy that preserves downstream
    annotations.
    """
    if not body.client_sample_keys:
        return DeletionsResponse(tombstoned=0)
    log.info(
        "healthkit_deletions_received_but_not_processed",
        identifier=body.identifier,
        count=len(body.client_sample_keys),
    )
    return DeletionsResponse(tombstoned=0)
