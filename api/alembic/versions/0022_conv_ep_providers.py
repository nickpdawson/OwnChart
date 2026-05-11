"""Conversations, episodes, and per-user LLM provider credentials.

Revision ID: 0022_conv_ep_providers
Revises: 0021_fact_significance
Create Date: 2026-05-11 (afternoon)

Per docs/10 + Nick's afternoon direction: OwnChart's primary objects
are Questions, Conversations, Moments, Episodes, Patterns, Dossiers,
Sources, Facts. This migration adds the three that don't yet exist:

  - conversations + conversation_messages + conversation_citations
  - episodes + episode_members (canonical, user-confirmable groupings
    of facts/sources/events around one real-world life moment — the
    "the example surgery" object)
  - llm_provider_credentials (multi-provider abstraction; per-user
    BYOK alongside the admin-provided default)

Each table carries the audit fields docs/10 calls out: privacy mode,
provider/model, prompt version, scope, references back to source IDs,
fact IDs, anchor IDs, candidate IDs. The contract is "every AI answer
is reproducible from this row."
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_conv_ep_providers"
down_revision: Union[str, None] = "0021_fact_significance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Conversations ---------------------------------------------------
    # One row per saved AI thread. Scope captures what the user asked
    # the conversation to be about: whole record, time window, source,
    # dossier, episode. Searchable via title / messages.
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(512)),
        # ask | make_sense | episode_intelligence | dossier_followup |
        # source_followup | review_triage
        sa.Column("kind", sa.String(48), nullable=False, server_default="ask", index=True),
        # JSON shape per docs/10:
        #   { "type": "whole_record" | "period" | "source" | "dossier"
        #             | "episode" | "fact",
        #     "period": {"from": "...", "to": "..."} | null,
        #     "source_ids": [uuid, ...], "topic_slug": "...",
        #     "episode_id": uuid, "anchor_fact_id": uuid }
        sa.Column("scope", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{\"type\": \"whole_record\"}'::jsonb")),
        # Provider / model used for the most recent assistant message.
        # Same fields live on conversation_messages — this is a denorm
        # so the list view can render "Claude" vs "GPT" without joining.
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(128)),
        sa.Column("privacy_mode", sa.String(32)),
        # archived | starred conversations are user-managed.
        sa.Column("starred", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("archived", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # user | assistant | system | tool
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String, nullable=False),
        # If assistant role: provider/model used + prompt_version + the
        # ModelRun that produced this message. user/system rows leave
        # these null.
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_version", sa.String(255)),
        sa.Column("privacy_mode", sa.String(32)),
        sa.Column("model_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("model_runs.id", ondelete="SET NULL")),
        # Structured output bag for assistant messages that emit a tool
        # (Episode Intelligence sections, Make Sense candidates, etc.).
        sa.Column("structured_output", postgresql.JSONB),
        # Token usage from the assistant call.
        sa.Column("usage", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_conv_created",
        "conversation_messages",
        ["conversation_id", "created_at"],
    )

    # One row per cited piece of evidence. Lets the UI render
    # citations as chips beneath an assistant message and the audit
    # log answer "what evidence supported this answer."
    op.create_table(
        "conversation_citations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversation_messages.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # fact | source | anchor | episode | candidate | event
        sa.Column("citation_type", sa.String(32), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        # source_backed | user_canonical | inferred | statistical | unknown
        # (docs/08 §340 claim labels)
        sa.Column("claim_label", sa.String(32)),
        sa.Column("excerpt", sa.String),
        sa.Column("note", sa.String(512)),
        sa.Column("ordinal", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    # --- Episodes --------------------------------------------------------
    # A canonical real-world life moment ("the example surgery").
    # Members are the underlying facts / sources / candidates that
    # make up the episode. The episode itself is user-confirmable;
    # LLM-proposed episodes become candidates first and are promoted
    # to canonical episodes via explicit user action.
    op.create_table(
        "episodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.String),
        # surgery | injury | illness | pregnancy | hospital_stay |
        # workup | recovery_window | travel | other
        sa.Column("kind", sa.String(48), nullable=False, server_default="other", index=True),
        sa.Column("date_start", sa.DateTime(timezone=True), index=True),
        sa.Column("date_end", sa.DateTime(timezone=True)),
        sa.Column("primary_fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("extracted_facts.id", ondelete="SET NULL")),
        # If promoted from a SensemakingCandidate, this points back so
        # the audit trail explains how the canonical episode came to be.
        sa.Column("promoted_from_candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sensemaking_candidates.id", ondelete="SET NULL")),
        # heuristic | llm | user | imported (e.g. FHIR Encounter rollup)
        sa.Column("created_by", sa.String(16), nullable=False, server_default="user"),
        # Open-ended payload: recovery_windows[], notable_metrics{},
        # interpretation_text, follow_up_questions[], etc. Schema lives
        # in the Episode Intelligence prompt / planner output.
        sa.Column("payload", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "episode_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("episode_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("episodes.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # fact | source | event | candidate | conversation
        sa.Column("member_type", sa.String(32), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        # primary | component | context | followup | recovery_metric
        sa.Column("role", sa.String(48), nullable=False, server_default="component"),
        sa.Column("ordinal", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("note", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "episode_id", "member_type", "subject_id",
            name="uq_episode_members_unique",
        ),
    )

    # --- LLM provider credentials ---------------------------------------
    # Per-user BYOK. Encrypted at rest with the same envelope the
    # OAuth tokens use (core/crypto.py). The admin can also store
    # a deployment-wide credential row with user_id IS NULL — that's
    # the default the user inherits.
    op.create_table(
        "llm_provider_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), index=True),
        # anthropic | openai | gemini | local | azure_openai
        sa.Column("provider", sa.String(32), nullable=False, index=True),
        # api_key | oauth | local_endpoint
        sa.Column("auth_kind", sa.String(32), nullable=False),
        # When auth_kind == api_key, this is the encrypted blob.
        sa.Column("encrypted_secret", sa.LargeBinary),
        # When auth_kind == local_endpoint, the base URL (no auth).
        sa.Column("endpoint_url", sa.String(512)),
        # OAuth: refresh + access tokens encrypted; expires_at for renewal.
        sa.Column("encrypted_refresh_token", sa.LargeBinary),
        sa.Column("encrypted_access_token", sa.LargeBinary),
        sa.Column("oauth_expires_at", sa.DateTime(timezone=True)),
        # Friendly label so the user can name multiple keys per provider.
        sa.Column("label", sa.String(128)),
        # Per-credential default model (falls through to provider default).
        sa.Column("default_model", sa.String(128)),
        # Capability flags surfaced to the UI: vision, long_context,
        # structured_outputs, local. JSON for forward compat.
        sa.Column("capabilities", postgresql.JSONB,
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_llm_provider_credentials_user_provider",
        "llm_provider_credentials",
        ["user_id", "provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_provider_credentials_user_provider",
                  table_name="llm_provider_credentials")
    op.drop_table("llm_provider_credentials")
    op.drop_table("episode_members")
    op.drop_table("episodes")
    op.drop_table("conversation_citations")
    op.drop_index("ix_conversation_messages_conv_created",
                  table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
