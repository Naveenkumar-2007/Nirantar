"""Narrow platform roles.

nirantar_relay: reads unpublished outbox rows across tenants and marks them
published. It has no access to any other table.

Revision ID: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nirantar_relay') THEN
            CREATE ROLE nirantar_relay LOGIN PASSWORD 'nirantar_relay' NOSUPERUSER;
          END IF;
        END $$;
        GRANT CONNECT ON DATABASE nirantar TO nirantar_relay;
        GRANT USAGE ON SCHEMA events TO nirantar_relay;
        GRANT SELECT, UPDATE (published_at) ON events.outbox TO nirantar_relay;
        CREATE POLICY relay_all_tenants ON events.outbox TO nirantar_relay USING (true) WITH CHECK (true);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS relay_all_tenants ON events.outbox;
        REVOKE ALL ON events.outbox FROM nirantar_relay;
        REVOKE USAGE ON SCHEMA events FROM nirantar_relay;
        """
    )
