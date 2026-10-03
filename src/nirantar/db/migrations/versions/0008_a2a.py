"""A2A: trusted agent cards, tasks, signed messages (with replay protection).

Revision ID: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.a2a_trusted_agents (
          tenant_id    text NOT NULL,
          agent_name   text NOT NULL,
          organization text NOT NULL,
          public_key   text NOT NULL,          -- base64 Ed25519 raw public key
          skills       text[] NOT NULL,
          card         jsonb NOT NULL,
          added_at     timestamptz NOT NULL DEFAULT now(),
          revoked_at   timestamptz,
          PRIMARY KEY (tenant_id, agent_name)
        );
        CREATE TABLE ops.a2a_tasks (
          tenant_id    text NOT NULL,
          task_id      text NOT NULL,
          skill        text NOT NULL,
          counterparty text NOT NULL,
          direction    text NOT NULL CHECK (direction IN ('inbound','outbound')),
          state        text NOT NULL CHECK (state IN ('submitted','working','input-required','completed','failed','canceled')),
          request      jsonb NOT NULL,
          result       jsonb,
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, task_id)
        );
        CREATE TABLE ops.a2a_messages (
          tenant_id    text NOT NULL,
          message_id   text NOT NULL,
          task_id      text NOT NULL,
          sender       text NOT NULL,
          nonce        text NOT NULL,
          kind         text NOT NULL,
          body         jsonb NOT NULL,
          signature    text NOT NULL,
          verified     boolean NOT NULL,
          received_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, message_id),
          UNIQUE (tenant_id, sender, nonce)
        );
        """
    )
    for table in ("ops.a2a_trusted_agents", "ops.a2a_tasks", "ops.a2a_messages"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.a2a_messages, ops.a2a_tasks, ops.a2a_trusted_agents;")
