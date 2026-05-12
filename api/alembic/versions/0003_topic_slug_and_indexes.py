"""topic slug + claim text trigram + seed example topic

Revision ID: 0003_topic_slug_and_indexes
Revises: 0002_image_ingest_fields
Create Date: 2026-05-08

- Adds `topics.slug` (unique) for clean dossier URLs.
- Adds a pg_trgm GIN index on `extracted_claims.label` to support fuzzy
  retrieval in /ask without requiring embeddings yet.
- Seeds a worked-example "Knee" topic so a fresh install shows the
  dossier shape immediately. Delete it from /topics if it does not
  apply.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_topic_slug_and_indexes"
down_revision: Union[str, None] = "0002_image_ingest_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("slug", sa.String(255)))
    op.execute("UPDATE topics SET slug = lower(regexp_replace(name, '[^a-zA-Z0-9]+', '-', 'g')) WHERE slug IS NULL")
    op.alter_column("topics", "slug", nullable=False)
    op.create_index("ix_topics_slug", "topics", ["slug"], unique=True)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_claims_label_trgm "
        "ON extracted_claims USING gin (label gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extracted_claims_description_trgm "
        "ON extracted_claims USING gin (description gin_trgm_ops)"
    )

    # Seed the Strabismus topic. Aliases are common variants the trigram
    # search alone might miss. Raw SQL because SQLAlchemy's prefix_with
    # places ON CONFLICT before INTO (invalid Postgres) — and we need
    # server-side gen_random_uuid() + now() defaults.
    op.execute(
        """
        INSERT INTO topics (id, name, slug, aliases, description, related_concepts, created_at, updated_at)
        VALUES (
            gen_random_uuid(),
            'Knee',
            'knee',
            ARRAY['knee pain','meniscus','ACL','knee injury','knee surgery'],
            'A worked-example dossier topic shipped with fresh installs. Delete it from /topics if it does not apply to you.',
            ARRAY['orthopedics','knee surgery','meniscectomy','ACL reconstruction','physical therapy'],
            now(),
            now()
        )
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM topics WHERE slug = 'knee'")
    op.execute("DROP INDEX IF EXISTS ix_extracted_claims_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_extracted_claims_label_trgm")
    op.drop_index("ix_topics_slug", table_name="topics")
    op.drop_column("topics", "slug")
