"""Readiness probe: the API role may read which migration the database is at (nothing else in that table exists).

Revision ID: 0031
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO nirantar_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM nirantar_app")
