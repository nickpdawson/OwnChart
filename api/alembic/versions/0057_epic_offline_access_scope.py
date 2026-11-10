"""Add offline_access to stored Epic connector scopes.

Revision ID: 0057_epic_offline_access_scope
Revises: 0056_email_ci_unique
Create Date: 2026-11-10

Connector rows snapshot the default scope string at create time, so the
Epic rows on every running instance still carry
``openid fhirUser launch/patient patient/*.read`` — no offline_access, so
Epic never issued a refresh token and every Epic connection died one hour
after the user connected (silently, until 2026-11-10: the fetcher swallowed
the 401s and the UI kept saying "connected").

This appends ``offline_access`` to Epic rows that lack it. Epic accepts the
scope from a public PKCE client (it is echoed in the granted scope) but only
issues a refresh_token to a confidential client, so the operator must also
set OWNCHART_EPIC_CLIENT_SECRET for this to matter. Rows the operator has
already edited to include offline_access are left alone. Other vendors are
untouched: Athena and Cerner gate offline_access behind separate approvals
and fail the whole authorize request when it is requested unapproved.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0057_epic_offline_access_scope"
down_revision = "0056_email_ci_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE provider_connectors "
            "SET scopes = scopes || ' offline_access' "
            "WHERE lower(coalesce(ehr_vendor, '')) = 'epic' "
            "AND position('offline_access' in scopes) = 0"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE provider_connectors "
            "SET scopes = btrim(replace(scopes, ' offline_access', '')) "
            "WHERE lower(coalesce(ehr_vendor, '')) = 'epic'"
        )
    )
