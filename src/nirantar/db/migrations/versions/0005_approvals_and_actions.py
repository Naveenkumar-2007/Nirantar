"""Approval tokens are single-use; record who requested each approval (maker-checker).

Revision ID: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ops.approvals ADD COLUMN requested_by text NOT NULL DEFAULT 'unknown';
        ALTER TABLE ops.approvals ADD COLUMN tool_name text;
        ALTER TABLE ops.approvals ADD COLUMN params_hash text;
        ALTER TABLE ops.approvals ADD COLUMN used_at timestamptz;
        ALTER TABLE ops.approvals DROP CONSTRAINT approvals_status_check;
        ALTER TABLE ops.approvals ADD CONSTRAINT approvals_status_check
          CHECK (status IN ('pending','granted','denied','expired','used'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ops.approvals DROP CONSTRAINT approvals_status_check;
        ALTER TABLE ops.approvals ADD CONSTRAINT approvals_status_check
          CHECK (status IN ('pending','granted','denied','expired'));
        ALTER TABLE ops.approvals DROP COLUMN used_at, DROP COLUMN params_hash, DROP COLUMN tool_name,
          DROP COLUMN requested_by;
        """
    )
