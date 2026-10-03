"""Daily at-risk snapshot per tenant (P4, ADR-0014): the scored active subscribers above the tenant's threshold.

Revision ID: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE ai.at_risk_snapshots (
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          snapshot_id  text NOT NULL,
          computed_at  timestamptz NOT NULL,
          summary      jsonb NOT NULL,       -- active, scored, unscored, threshold
          items        jsonb NOT NULL,       -- list of entity_id, customer_id, p_churn_60d, p_payment_driven, route
          PRIMARY KEY (tenant_id, snapshot_id)
        );
        CREATE INDEX at_risk_latest ON ai.at_risk_snapshots (tenant_id, computed_at DESC);
        ALTER TABLE ai.at_risk_snapshots ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ai.at_risk_snapshots FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON ai.at_risk_snapshots USING (tenant_id = {CURRENT})
          WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT ON ai.at_risk_snapshots TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.at_risk_snapshots;")
