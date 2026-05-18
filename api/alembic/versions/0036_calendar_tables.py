"""Calendar ingest tables — calendar_sources + calendar_events (M02 Slice 3).

Revision ID: 0036_calendar_tables
Revises: 0035_extracted_fact_raw_metadata
Create Date: 2026-05-18

Beta 1 Milestone 02, Slice 3 — EventKit calendar ingest (iOS only).

What this lands:

  calendar_sources
    - One row per (user, person_record, adapter, external calendar id).
    - `adapter_type` is "ios_eventkit" today. Google / ICS / CalDAV
      stay out of scope; the column is reserved so they can land
      without a schema migration.
    - `privacy_mode` controls what fields are STORED on
      calendar_events ("full_details" | "title_and_time" |
      "busy_only"). The server enforces redaction at ingest as
      defense in depth; iOS is expected to apply it client-side
      first.
    - `llm_full_details_consent` is the SECOND elevation (PM B-4).
      Even when full title/location/notes are stored, the Ask
      retrieval projector hides them unless this flag is true on the
      owning source. Default false — the LLM exposure floor is
      busy-only-equivalent until the user explicitly elevates.
    - `disconnected_at` is the soft-disconnect timestamp. Disconnect
      cascades a tombstone to every event on the source (route-side,
      not DB-side; ON DELETE CASCADE only fires if the source row is
      hard-deleted).

  calendar_events
    - One row per (calendar_source_id, external_id). EventKit emits
      stable identifiers per CalendarEventKit calendar; subsequent
      ingest batches upsert by this key.
    - `start_at` / `end_at` always populated. `all_day` reflects the
      EventKit `isAllDay` flag — when true, the times still land at
      the start/end of the relevant local day per iOS-side
      normalization (server doesn't second-guess timezone math).
    - `title` / `location` / `notes` / `attendees_count` are
      stored ONLY when `privacy_mode_applied` allows. Lower modes
      land as NULL — the row's existence is the "busy" signal.
    - `privacy_mode_applied` records what mode was in effect when
      the row was last upserted. A subsequent privacy_mode tightening
      on the source triggers a redaction sweep (route-side).
    - `tombstoned_at` (null = visible) is the soft-delete marker.
      Retrieval filters it out; a periodic worker hard-deletes rows
      whose tombstoned_at is older than 30 days (PM B-3).
    - `raw_metadata` carries iOS-side context that doesn't deserve a
      promoted column today (recurrence pattern, attendee role,
      EventKit calendar color). Same JSONB-bag convention as
      ExtractedFact.raw_metadata from Slice 2.

Indexes (one per real query path):
  - calendar_sources: unique on (user_id, person_record_id,
    adapter_type, external_id) so iOS re-picking the same calendar
    is a no-op upsert; partial index on (person_record_id) WHERE
    disconnected_at IS NULL for the "active sources" lookup the
    settings UI runs every page load.
  - calendar_events: unique on (calendar_source_id, external_id) for
    upsert; (person_record_id, start_at) WHERE tombstoned_at IS NULL
    for the time-window retrieval query Ask uses; (tombstoned_at)
    WHERE tombstoned_at IS NOT NULL for the 30d purge worker's scan.

Both tables carry `person_record_id` from the start (Slice 1
perimeter). Migration 0029's column-add / 0031's NOT-NULL chain is
unnecessary because these tables are *born* multi-person-aware.

NOT NULL person_record_id on both. NOT NULL adapter_type,
external_id, display_name on sources. NOT NULL start_at, end_at,
privacy_mode_applied, external_id, external_modified_at on events.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic revision identifiers.
revision = "0036_calendar_tables"
down_revision = "0035_extracted_fact_raw_metadata"
branch_labels = None
depends_on = None


_PRIVACY_MODES = ("full_details", "title_and_time", "busy_only")
_ADAPTER_TYPES = ("ios_eventkit",)  # Future: "google", "ics", "caldav"


def upgrade() -> None:
    op.create_table(
        "calendar_sources",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("adapter_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column(
            "privacy_mode", sa.String(16),
            nullable=False, server_default="title_and_time",
        ),
        sa.Column(
            "llm_full_details_consent", sa.Boolean,
            nullable=False, server_default=sa.text("false"),
        ),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "privacy_mode IN ('full_details','title_and_time','busy_only')",
            name="calendar_sources_privacy_mode_chk",
        ),
        sa.CheckConstraint(
            "adapter_type IN ('ios_eventkit')",
            name="calendar_sources_adapter_type_chk",
        ),
        sa.UniqueConstraint(
            "user_id", "person_record_id", "adapter_type", "external_id",
            name="calendar_sources_user_record_adapter_external_uq",
        ),
    )
    op.create_index(
        "calendar_sources_active_idx",
        "calendar_sources",
        ["person_record_id"],
        postgresql_where=sa.text("disconnected_at IS NULL"),
    )

    op.create_table(
        "calendar_events",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "person_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "calendar_source_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column(
            "external_modified_at", sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "all_day", sa.Boolean,
            nullable=False, server_default=sa.text("false"),
        ),
        # Stored only when privacy_mode_applied allows; NULL otherwise.
        sa.Column("title", sa.String(512)),
        sa.Column("location", sa.String(512)),
        sa.Column("notes", sa.String),
        sa.Column("attendees_count", sa.SmallInteger),
        sa.Column(
            "privacy_mode_applied", sa.String(16), nullable=False,
        ),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True)),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "privacy_mode_applied IN ('full_details','title_and_time','busy_only')",
            name="calendar_events_privacy_mode_applied_chk",
        ),
        sa.UniqueConstraint(
            "calendar_source_id", "external_id",
            name="calendar_events_source_external_uq",
        ),
    )
    op.create_index(
        "calendar_events_record_time_idx",
        "calendar_events",
        ["person_record_id", "start_at"],
        postgresql_where=sa.text("tombstoned_at IS NULL"),
    )
    op.create_index(
        "calendar_events_tombstoned_idx",
        "calendar_events",
        ["tombstoned_at"],
        postgresql_where=sa.text("tombstoned_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("calendar_events_tombstoned_idx", table_name="calendar_events")
    op.drop_index("calendar_events_record_time_idx", table_name="calendar_events")
    op.drop_table("calendar_events")
    op.drop_index("calendar_sources_active_idx", table_name="calendar_sources")
    op.drop_table("calendar_sources")
