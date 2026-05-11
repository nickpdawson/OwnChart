"""topic slug + claim text trigram + seed strabismus

Revision ID: 0003_topic_slug_and_indexes
Revises: 0002_image_ingest_fields
Create Date: 2026-05-08

- Adds `topics.slug` (unique) for clean dossier URLs.
- Adds a pg_trgm GIN index on `extracted_claims.label` to support fuzzy
  retrieval in /ask without requiring embeddings yet.
- Seeds the Strabismus topic.
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
            'Strabismus',
            'strabismus',
            ARRAY['strabismus','esotropia','exotropia','lazy eye','crossed eyes','ocular misalignment'],
            'Misalignment of the eyes; the V1 proof case.',
            ARRAY['ophthalmology','eye muscle surgery','eye patching','amblyopia','diplopia','orthoptics'],
            now(),
            now()
        )
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM topics WHERE slug = 'strabismus'")
    op.execute("DROP INDEX IF EXISTS ix_extracted_claims_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_extracted_claims_label_trgm")
    op.drop_index("ix_topics_slug", table_name="topics")
    op.drop_column("topics", "slug")
