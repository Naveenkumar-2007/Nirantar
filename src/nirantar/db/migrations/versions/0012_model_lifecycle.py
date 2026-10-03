"""Per-tenant model lifecycle (P3, ADR-0013): versions with rollout stage, stage events, monitoring reports.

Revision ID: 0012
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ai.model_versions (
          tenant_id      text NOT NULL REFERENCES core.tenants(tenant_id),
          model_name     text NOT NULL,                  -- e.g. m1_debit_failure
          version        text NOT NULL,                  -- MLflow registered-model version
          registry_name  text NOT NULL,                  -- MLflow name, e.g. m1_debit_failure.<tenant>
          stage          text NOT NULL CHECK (stage IN ('rejected','shadow','canary','champion','retired')),
          feature_set    text NOT NULL,
          algorithm      text NOT NULL,
          data_source    text NOT NULL CHECK (data_source IN ('tenant','synthetic')),
          metrics        jsonb NOT NULL,                 -- test metrics of the selected model
          baseline       jsonb NOT NULL,                 -- test metrics of the prior (cold-start) model
          gates          jsonb NOT NULL,                 -- {"passed": bool, "failures": [...]}
          training       jsonb NOT NULL,                 -- rows, window, split, snapshot id
          canary_share   double precision NOT NULL DEFAULT 0 CHECK (canary_share BETWEEN 0 AND 1),
          trained_at     timestamptz NOT NULL,
          stage_changed_at timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, model_name, version)
        );
        -- at most one champion and one canary per tenant+model
        CREATE UNIQUE INDEX model_one_champion ON ai.model_versions (tenant_id, model_name) WHERE stage = 'champion';
        CREATE UNIQUE INDEX model_one_canary ON ai.model_versions (tenant_id, model_name) WHERE stage = 'canary';

        CREATE TABLE ai.model_events (
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          event_id    text NOT NULL,
          model_name  text NOT NULL,
          version     text NOT NULL,
          from_stage  text,
          to_stage    text NOT NULL,
          reason      text NOT NULL,
          evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
          actor       text NOT NULL,
          at          timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, event_id)
        );
        CREATE TRIGGER model_events_append_only BEFORE UPDATE OR DELETE ON ai.model_events
          FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();

        CREATE TABLE ai.model_monitoring (
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          report_id   text NOT NULL,
          model_name  text NOT NULL,
          computed_at timestamptz NOT NULL,
          report      jsonb NOT NULL,                 -- live performance per version, drift, triggers
          PRIMARY KEY (tenant_id, report_id)
        );
        CREATE INDEX model_monitoring_latest ON ai.model_monitoring (tenant_id, model_name, computed_at DESC);
    """)
    for table, grants in (("ai.model_versions", "SELECT, INSERT, UPDATE"), ("ai.model_events", "SELECT, INSERT"),
                          ("ai.model_monitoring", "SELECT, INSERT")):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT {grants} ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.model_monitoring, ai.model_events, ai.model_versions;")
