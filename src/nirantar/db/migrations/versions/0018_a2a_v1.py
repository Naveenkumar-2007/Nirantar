"""A2A v1.0 server (P7, ADR-0016): tasks persist the protocol Task, its context, owner and the gateway action.

Revision ID: 0018
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ops.a2a_tasks
          ADD COLUMN context_id text,
          ADD COLUMN action_id text,
          ADD COLUMN task_proto jsonb;
        ALTER TABLE ops.a2a_tasks DROP CONSTRAINT a2a_tasks_state_check;
        ALTER TABLE ops.a2a_tasks ADD CONSTRAINT a2a_tasks_state_check CHECK (state IN
          ('submitted','working','input-required','auth-required','completed','failed','canceled','rejected'));
        CREATE INDEX a2a_tasks_owner_idx ON ops.a2a_tasks (tenant_id, counterparty, updated_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS ops.a2a_tasks_owner_idx;
        DELETE FROM ops.a2a_tasks WHERE state IN ('auth-required','rejected');
        ALTER TABLE ops.a2a_tasks DROP CONSTRAINT a2a_tasks_state_check;
        ALTER TABLE ops.a2a_tasks ADD CONSTRAINT a2a_tasks_state_check CHECK (state IN
          ('submitted','working','input-required','completed','failed','canceled'));
        ALTER TABLE ops.a2a_tasks DROP COLUMN context_id, DROP COLUMN action_id, DROP COLUMN task_proto;
    """)
