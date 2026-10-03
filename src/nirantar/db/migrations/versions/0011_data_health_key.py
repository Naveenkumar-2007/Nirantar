"""ai.data_health is keyed by run, not by time: two runs can share a business `now` (found by the P2 re-run test).

Revision ID: 0011
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ai.data_health DROP CONSTRAINT data_health_pkey;
        ALTER TABLE ai.data_health ADD PRIMARY KEY (tenant_id, run_id);
        CREATE INDEX data_health_latest ON ai.data_health (tenant_id, computed_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS ai.data_health_latest;
        ALTER TABLE ai.data_health DROP CONSTRAINT data_health_pkey;
        ALTER TABLE ai.data_health ADD PRIMARY KEY (tenant_id, computed_at);
    """)
