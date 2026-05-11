"""Auto-confirm Claude-vision facts at high+medium confidence.

Revision ID: 0008_auto_confirm_vision_facts
Revises: 0007_extraction_jobs
Create Date: 2026-05-09

The original vision pipeline parked every extracted fact in
`review_state='needs_review'`, which meant the user had to manually
click-confirm 100+ correctly-extracted facts ("Bruce T. Carter, M.D.",
"Left lateral rectus recession 5 mm", "Esotropia 10 prism diopter")
from a 1984 ophthalmology fax. That's gatekeeper-not-research-partner
UX and contradicts docs/04: "Unreviewed extracted facts appear
immediately on timelines, but they must be visibly badged" — *badged*,
not click-confirmed.

This migration retroactively reclassifies vision-extracted facts using
the same numeric confidence the pipeline already wrote:

  high (90)   → confirmed
  medium (65) → confirmed
  low  (35)   → needs_review (genuine ambiguity)
  possible (20) → needs_review
  null/unknown → needs_review

User-canonical state from UserAssertion is preserved by leaving any
fact whose state is currently 'corrected' or 'rejected' alone.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008_auto_confirm_vision_facts"
down_revision: Union[str, None] = "0007_extraction_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE extracted_facts
        SET review_state = 'confirmed'
        WHERE review_state = 'needs_review'
          AND extraction_method = 'claude_vision_v1'
          AND confidence IS NOT NULL
          AND confidence >= 60
        """
    )


def downgrade() -> None:
    # No-op: we cannot tell apart facts auto-confirmed by this migration
    # vs. user-confirmed in the same window. Roll forward instead.
    pass
