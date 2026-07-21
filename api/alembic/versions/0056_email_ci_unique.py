"""Case-insensitive unique email (P1-9).

Revision ID: 0056_email_ci_unique
Revises: 0055_invitation_pending_accept
Create Date: 2026-07-20

P1-9 (2026-07-20 release review) — registration/login/reset now normalize
addresses (strip + lowercase) and look them up case-insensitively. To make
that a hard invariant (and stop two accounts differing only by case —
``Nick@x`` vs ``nick@x``) this adds a UNIQUE functional index on
``lower(email)``. The plain ``users.email`` unique constraint (exact case)
stays; this adds the case-insensitive guarantee on top.

PRE-MIGRATION GUARD (operator action required on collision):
  Before creating the index we check for existing rows that already collide
  case-insensitively. If any exist the migration FAILS with a clear message
  listing every colliding email and its user ids. There is no safe automatic
  winner between two real accounts sharing an address — the operator must
  merge or rename them, then re-run the upgrade. Existing rows are NOT
  rewritten to lowercase (case-insensitive lookups handle legacy case), so a
  clean instance upgrades with zero data change.

Postgres-only (functional index + ``array_agg``); OwnChart ships on Postgres.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0056_email_ci_unique"
down_revision = "0055_invitation_pending_accept"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    collisions = bind.execute(
        sa.text(
            "SELECT lower(email) AS key, array_agg(id::text) AS ids "
            "FROM users GROUP BY lower(email) HAVING count(*) > 1 "
            "ORDER BY lower(email)"
        )
    ).fetchall()
    if collisions:
        detail = "; ".join(
            f"{row.key!r} -> {', '.join(row.ids)}" for row in collisions
        )
        raise RuntimeError(
            "Migration 0056 cannot add a case-insensitive unique email index: "
            "existing users collide case-insensitively. Resolve each of these "
            "(merge or rename the duplicate accounts) and re-run the upgrade. "
            f"Colliding email -> user ids: {detail}"
        )

    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")
