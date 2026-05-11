"""Topic label_patterns — regex membership beyond alias substring.

Revision ID: 0009_topic_label_patterns
Revises: 0008_auto_confirm_vision_facts
Create Date: 2026-05-09

Topic resolution was alias-substring-only: a fact joined a topic's
dossier iff its label or description contained one of the topic's
aliases. That misses anatomically-described facts (Nick's strabismus
surgeries are recorded as "Left lateral rectus recession 5 mm" or
"Anterior recession and anteriorization of right inferior oblique" —
nothing in the label says "strabismus"). The result: the dossier
showed 5 procedures but 10 actual operative entries existed in the DB.

Systemic fix: every Topic also carries a list of `label_patterns`
(Postgres POSIX regex strings). The resolver OR-matches alias
substring against label_patterns, which lets a topic capture a whole
class of variants without exploding the alias list. New topics
declare their own patterns; old topics keep working unchanged
(default empty array).

Strabismus topic is seeded with patterns covering the eye-muscle
surgery vocabulary that operative reports use:
  - (lateral|medial|superior|inferior) rectus
  - (superior|inferior) oblique
  - posterior fixation suture
  - extraocular muscle
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_topic_label_patterns"
down_revision: Union[str, None] = "0008_auto_confirm_vision_facts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "topics",
        sa.Column(
            "label_patterns",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )

    # Seed Strabismus with the eye-muscle vocabulary that operative
    # reports use. Patterns are POSIX case-insensitive regex (matched
    # via Postgres `~*`); kept narrow to avoid pulling in unrelated
    # ophthalmology entries.
    op.execute(
        r"""
        UPDATE topics
        SET label_patterns = ARRAY[
            '(lateral|medial|superior|inferior)\s+rectus',
            '(superior|inferior)\s+oblique',
            'posterior\s+fixation\s+suture',
            'extraocular\s+muscle',
            'recession\s+and\s+anteriorization'
        ]
        WHERE slug = 'strabismus'
        """
    )


def downgrade() -> None:
    op.drop_column("topics", "label_patterns")
