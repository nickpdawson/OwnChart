"""Agreement-acceptance evidence (GDPR Art. 9 explicit consent).

Revision ID: 0052_agreement_acceptances
Revises: 0051_password_reset_and_email_verification
Create Date: 2026-07-12

Adds ``agreement_acceptances`` — an append-only record of a tester
accepting the consent / tester agreement. One row per acceptance event;
the current state is the most-recent row per user. ``consent_flags`` is
the verbatim set of checkboxes ticked; ``agreement_version`` is an opaque
operator-defined string so new agreement revisions need no schema change.

CASCADE on ``user_id`` so Art. 17 account erasure removes the consent
evidence along with the subject it describes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_agreement_acceptances"
down_revision = "0051_password_reset_and_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agreement_acceptances",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agreement_version", sa.String(255), nullable=False),
        sa.Column(
            "consent_flags", postgresql.JSONB(),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("context", sa.String(32), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agreement_acceptances_user_id",
        "agreement_acceptances",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agreement_acceptances_user_id",
        table_name="agreement_acceptances",
    )
    op.drop_table("agreement_acceptances")
