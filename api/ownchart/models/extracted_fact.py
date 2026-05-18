import uuid
from datetime import datetime, timezone

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_uuid


class ExtractedFact(Base, TimestampMixin):
    """A machine-extracted or parsed clinical fact. Always carries provenance.

    Renamed from ExtractedClaim — `claim` is billing-overloaded in healthcare
    (FHIR Claim is the insurance billing resource). OwnChart's
    ExtractedFact is a clinical assertion, not a billing artifact.

    Numeric `confidence` is internal; the UI renders the readable label
    derived in the API layer.
    """

    __tablename__ = "extracted_facts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    # M02 perimeter: the person record this fact belongs to. Added
    # by migration 0029 (NOT NULL via 0031 after backfill). Every
    # record-scoped read and the M02-rollout perimeter SELECTs
    # filter on this column; without the model declaration the
    # SELECTs raise AttributeError at request time. Registered as
    # part of Batch 9 backfill after the gap was discovered.
    person_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person_records.id", ondelete="CASCADE"),
    )

    # condition | procedure | medication | symptom | finding | observation |
    # encounter | imaging_study | lab_result | provider_relationship |
    # life_context_event | inferred_relationship
    fact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    label: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(String)

    date_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # day | month | year | decade | unknown
    date_precision: Mapped[str | None] = mapped_column(String(16))

    body_site: Mapped[str | None] = mapped_column(String(128))
    laterality: Mapped[str | None] = mapped_column(String(16))     # left | right | bilateral

    coded_concepts: Mapped[dict | None] = mapped_column(JSONB)     # {snomed:[...], icd10:[...], rxnorm:[...]}

    # Sample-level numeric / structured payload that does not belong
    # in coded_concepts. Added by migration 0035 (Slice 2). First
    # callsite: HealthKit workout path (BE-3 contract) writing
    # raw_metadata.healthkit = {duration_s, distance_m, energy_kcal,
    # source, device, sync_mode, sample_metadata}. Future ingest
    # paths (body/sleep, Auto Export numeric, EventKit timing) will
    # reuse the same column for the same reason: coded_concepts stays
    # the retrieval-keyed bag; raw_metadata stays observational.
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)

    confidence: Mapped[int | None] = mapped_column(Integer)        # 0-100 internal
    review_state: Mapped[str] = mapped_column(
        String(16), default="needs_review", nullable=False, index=True
    )

    evidence_anchor_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )

    # ocr_tesseract | claude_vision_v1 | ccda_xpath | fhir_resource | patient_self_report
    # | health_auto_export | native_healthkit
    extraction_method: Mapped[str] = mapped_column(String(64), nullable=False)

    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )

    # Idempotency key for client-pushed samples (native HealthKit sync,
    # future connectors). Partial unique index when non-null; NULL for
    # everything not pushed by a typed client (CCDA, photos, vision,
    # auto_export). Format is per-strategy:
    #   - daily_aggregate: "agg-<HKIdentifier>-<YYYY-MM-DD>"
    #   - raw:             "sha256(identifier|start|end|value)"  (source-neutral)
    client_sample_key: Mapped[str | None] = mapped_column(String(128))

    # Cross-source equivalence (docs/07 §487-545). Same key across
    # multiple rows means "these are the same real-world thing seen by
    # different sources." Populated at ingest by canonical/equivalence.py
    # for daily-aggregate metrics; NULL for facts without a clean
    # canonicalization rule (medications, narrative claims, photos).
    # Non-unique partial index — duplicates ARE the design intent;
    # presentation collapses them, provenance keeps every row.
    equivalence_key: Mapped[str | None] = mapped_column(String(256))

    # Review reasons (docs/07 Priority 1 §634-640). Machine code +
    # human text co-exist so the UI can localize / restyle and the
    # backend can group by code. Populated at ingest where we can
    # confidently classify (vision-extracted contact names, Auto
    # Export Skipped medications); NULL for facts where the reason
    # isn't obvious enough to assert.
    why_needs_review_code: Mapped[str | None] = mapped_column(String(64))
    why_needs_review_text: Mapped[str | None] = mapped_column(String(512))
    # 1 = highest (conflicts between sources), 7 = lowest (metadata
    # cleanup). NULL = uncomputed (V1 rarely sets this).
    review_priority: Mapped[int | None] = mapped_column(SmallInteger)
    # Task verb the user is being asked to perform — e.g.
    # 'confirm_event', 'resolve_conflict', 'merge_duplicates',
    # 'keep_as_source_only', 'confirm_model_inference',
    # 'add_patient_context'. NULL when no specific task applies.
    review_task_type: Mapped[str | None] = mapped_column(String(48))
    # When true the Review Inbox shows a "Mark source-only" quick
    # action alongside Confirm/Defer/Reject. Set automatically for
    # confidently-low-value extractions (provider/contact names from
    # fax cover sheets, scheduling clerks, records custodians).
    source_context_only_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # docs/07 R5: LLM-assisted candidate label. UI prefers this when
    # present; original `label` stays immutable as the source-of-
    # truth for retrieval and audit. `display_label_method` records
    # how the candidate was produced — 'llm_v1' for the relabel
    # backfill, future values for heuristic or user-edited paths.
    display_label: Mapped[str | None] = mapped_column(String(512))
    display_label_method: Mapped[str | None] = mapped_column(String(32))

    # User-confirmable significance (2026-05-11). Every patient-facing
    # surface ranks by this — Home, Timeline, Discover, Fact Context.
    # Taxonomy (lower = louder in UI):
    #   major_event | major_diagnosis | major_procedure |
    #   major_medication | major_activity_lifestyle | background |
    #   source_only
    # `significance_source ∈ {user, llm, heuristic, default}`. User
    # always wins. See feedback_significance_layer.md.
    significance: Mapped[str | None] = mapped_column(String(48))
    significance_source: Mapped[str | None] = mapped_column(String(16))
    significance_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Auto-populate significance at ingest, without touching every
# ingest site. Listener fires before INSERT; if the row already has a
# significance (LLM-suggested, user-supplied), we never overwrite it.
# Computed values get `significance_source='heuristic'` so the
# downstream override hierarchy (user > llm > heuristic > default)
# stays meaningful.
@event.listens_for(ExtractedFact, "before_insert")
def _assign_default_significance(_mapper, _connection, target: ExtractedFact) -> None:
    if target.significance:
        return
    # Lazy import — significance module imports nothing from models,
    # so this is just to avoid an import cycle at app startup.
    from ..canonical.significance import compute as _compute_significance
    target.significance = _compute_significance(target)
    target.significance_source = "heuristic"
    target.significance_set_at = datetime.now(timezone.utc)
