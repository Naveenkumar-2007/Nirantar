"""Webhook secret reference per provider account, reconciliation discrepancies.

Revision ID: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE core.provider_accounts ADD COLUMN webhook_secret_ref text;
        ALTER TABLE core.provider_accounts ADD COLUMN last_reconciled_at timestamptz;

        CREATE TABLE billing.discrepancies (
          tenant_id            text NOT NULL,
          discrepancy_id       text NOT NULL,
          provider             text NOT NULL,
          provider_payment_id  text NOT NULL,
          kind                 text NOT NULL,       -- e.g. missing_locally, status_mismatch, amount_mismatch
          local_state          jsonb NOT NULL,
          provider_state       jsonb NOT NULL,
          detected_at          timestamptz NOT NULL DEFAULT now(),
          resolved_at          timestamptz,
          resolution           text,
          PRIMARY KEY (tenant_id, discrepancy_id)
        );
        ALTER TABLE billing.discrepancies ENABLE ROW LEVEL SECURITY;
        ALTER TABLE billing.discrepancies FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON billing.discrepancies
          USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON billing.discrepancies TO nirantar_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS billing.discrepancies;
        ALTER TABLE core.provider_accounts DROP COLUMN IF EXISTS webhook_secret_ref;
        ALTER TABLE core.provider_accounts DROP COLUMN IF EXISTS last_reconciled_at;
        """
    )
