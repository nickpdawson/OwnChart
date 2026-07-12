"""Per-user LLM cost attribution on model_runs (Batch B).

Revision ID: 0053_model_run_user_cost
Revises: 0052_agreement_acceptances
Create Date: 2026-07-12

Adds user/record attribution + a derived cost to ``model_runs`` so LLM
spend can be billed and capped per user:

  - ``user_id``            nullable FK → users, ON DELETE SET NULL
  - ``person_record_id``   nullable FK → person_records, ON DELETE SET NULL
  - ``estimated_cost_usd`` NUMERIC(12,6) — USD cost from the run's usage

Both FKs are SET NULL (not CASCADE): ``model_runs`` is append-only audit
evidence that must survive account/record erasure, and it carries no PHI
— only a foreign key that nulls out on delete.

Index ``ix_model_runs_user_created`` on ``(user_id, created_at)`` backs
the month-to-date spend SUM the monthly ceiling and /usage view run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053_model_run_user_cost"
down_revision = "0052_agreement_acceptances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_runs",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_runs",
        sa.Column("person_record_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_runs",
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
    )
    op.create_foreign_key(
        "fk_model_runs_user_id",
        "model_runs", "users",
        ["user_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_model_runs_person_record_id",
        "model_runs", "person_records",
        ["person_record_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_model_runs_user_created",
        "model_runs",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_runs_user_created", table_name="model_runs")
    op.drop_constraint("fk_model_runs_person_record_id", "model_runs", type_="foreignkey")
    op.drop_constraint("fk_model_runs_user_id", "model_runs", type_="foreignkey")
    op.drop_column("model_runs", "estimated_cost_usd")
    op.drop_column("model_runs", "person_record_id")
    op.drop_column("model_runs", "user_id")
