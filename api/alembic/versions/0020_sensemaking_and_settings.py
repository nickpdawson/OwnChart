"""Sensemaking jobs + candidates, user settings, audit events.

Revision ID: 0020_sensemaking_and_settings
Revises: 0019_display_label
Create Date: 2026-05-11

Foundation for docs/08 (LLM sensemaking jobs and guardrails) and
docs/09 (user profile, settings, and configuration).

Four new tables:

- `sensemaking_jobs` — one row per user-initiated or system-initiated
  Make Sense run. Links to ModelRun for the audit trail; carries the
  privacy mode the job ran under so we can answer "what was sent."

- `sensemaking_candidates` — polymorphic candidate table. V1 collapses
  the five candidate types from docs/08 into one shape with a
  `candidate_type` discriminator. Migrating later to per-type tables
  is straightforward (data → table); going the other way would be
  painful, so we start broad.

- `user_settings` — key/value JSONB store keyed off the registry in
  api/ownchart/settings/registry.yaml. The registry is the schema;
  this table is the values. One row per (user, key); absence means
  "use the default from the registry."

- `audit_events` — every settings change, every consent toggle, every
  sensemaking job runs through here. docs/09 §532: "All settings
  writes should create audit events."
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_sensemaking_and_settings"
down_revision: Union[str, None] = "0019_display_label"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sensemaking_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        # source_summary | episode_candidates | review_queue_triage |
        # label_translation | period_summary
        sa.Column("job_type", sa.String(48), nullable=False, index=True),
        # pending | running | completed | failed | refused
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        # off | metadata_only | selected_evidence | full_source
        sa.Column("privacy_mode", sa.String(32), nullable=False),
        sa.Column("scope", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("model_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("model_runs.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(2000)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "sensemaking_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sensemaking_jobs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # source_summary | episode | duplicate_group | review_task |
        # label_translation | topic | question
        sa.Column("candidate_type", sa.String(48), nullable=False, index=True),
        sa.Column("title", sa.String(512)),
        sa.Column("summary_text", sa.String),
        # Free-form body of the candidate: episode events, duplicate
        # row ids, suggested labels, etc. Keyed by candidate_type.
        sa.Column("payload", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        # source_backed | user_canonical | inferred | statistical |
        # unknown (docs/08 §340)
        sa.Column("claim_label", sa.String(32)),
        sa.Column("confidence", sa.SmallInteger),
        sa.Column("source_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=False, server_default=sa.text("'{}'::uuid[]")),
        sa.Column("fact_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=False, server_default=sa.text("'{}'::uuid[]")),
        sa.Column("evidence_anchor_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
                  nullable=False, server_default=sa.text("'{}'::uuid[]")),
        # pending | accepted | edited | dismissed | rejected
        sa.Column("disposition", sa.String(16), nullable=False,
                  server_default="pending", index=True),
        sa.Column("disposition_at", sa.DateTime(timezone=True)),
        sa.Column("user_edit", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    # Composite index for "give me the pending source_summary candidate
    # for source X" lookups (the source page reads this on every render
    # to know whether to show a draft or the "Make sense" button).
    op.create_index(
        "ix_sensemaking_candidates_type_disposition",
        "sensemaking_candidates",
        ["candidate_type", "disposition"],
    )

    op.create_table(
        "user_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        # Registry key, e.g. "ai.privacy_mode" or
        # "review.show_source_only_in_inbox". Schema lives in
        # api/ownchart/settings/registry.yaml.
        sa.Column("key", sa.String(128), nullable=False),
        # JSONB so a boolean, a string enum, and a number all fit.
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        # setting_change | consent_change | sensemaking_started |
        # sensemaking_completed | candidate_disposition |
        # source_only_marked | etc.
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("subject_type", sa.String(48)),  # "source", "fact", "setting", ...
        sa.Column("subject_id", sa.String(128)),
        sa.Column("detail", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("user_settings")
    op.drop_index("ix_sensemaking_candidates_type_disposition",
                  table_name="sensemaking_candidates")
    op.drop_table("sensemaking_candidates")
    op.drop_table("sensemaking_jobs")
