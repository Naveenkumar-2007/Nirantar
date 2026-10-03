"""Disputes (chargebacks) linked to debits.

Revision ID: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE billing.disputes (
          tenant_id            text NOT NULL,
          dispute_id           text NOT NULL,
          provider             text NOT NULL,
          provider_dispute_id  text NOT NULL,
          provider_payment_id  text NOT NULL,
          debit_id             text,
          amount_minor         bigint NOT NULL CHECK (amount_minor > 0),
          currency             text NOT NULL DEFAULT 'INR',
          reason_code          text NOT NULL,
          status               text NOT NULL CHECK (status IN ('open','evidence_ready','submitted','won','lost','accepted')),
          respond_by           timestamptz NOT NULL,
          created_at           timestamptz NOT NULL DEFAULT now(),
          updated_at           timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, dispute_id),
          UNIQUE (tenant_id, provider, provider_dispute_id)
        );
        ALTER TABLE billing.disputes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE billing.disputes FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON billing.disputes USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON billing.disputes TO nirantar_app;
        GRANT DELETE ON ai.memory TO nirantar_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.disputes; REVOKE DELETE ON ai.memory FROM nirantar_app;")
