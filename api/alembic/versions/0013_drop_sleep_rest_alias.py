"""Drop 'rest' alias from the Sleep topic seed (#50).

Revision ID: 0013_drop_sleep_rest_alias
Revises: 0012_seed_wearable_topics
Create Date: 2026-05-09

The seed in 0012 included `rest` as a Sleep alias on the theory that
"rest" is a synonym for sleep. In practice the membership predicate
substring-matches aliases against fact labels, so `rest` matched
"Resting energy" (Activity), "Resting HR" (Heart), and any clinical
"at rest" phrasing — pulling 30k+ Activity facts into the Sleep
dossier.

The fix is alias-only: Sleep's `label_patterns` (`^Sleep:`,
`^Sleep session`) already cover the wearable lane, and the
remaining aliases (`sleep`, `insomnia`, `sleep apnea`,
`sleep quality`) cover clinical mentions. `rest` carries no signal
that the other terms don't.

0012 has been edited to omit `rest` from fresh installs; this
migration removes it from already-deployed rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0013_drop_sleep_rest_alias"
down_revision: Union[str, None] = "0012_seed_wearable_topics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE topics SET aliases = array_remove(aliases, 'rest') "
        "WHERE slug = 'sleep'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE topics SET aliases = array_append(aliases, 'rest') "
        "WHERE slug = 'sleep' AND NOT ('rest' = ANY(aliases))"
    )
