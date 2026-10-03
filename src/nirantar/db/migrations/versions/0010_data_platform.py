"""Data platform (P2, ADR-0012): change capture columns, backfill checkpoints, pipeline runs, data health.

Change capture: every extracted table gets `cdc_txid xid8`, set by trigger to the writing transaction's id.
The extractor reads rows with cdc_txid >= its watermark and advances the watermark to the snapshot's xmin
(the oldest transaction still running). Rows written by a transaction that commits later always have
txid >= that xmin, so they are picked up next run — nothing is skipped, and duplicates are removed in silver.
Business timestamps (which can be historical, e.g. replays/backfills) are never used as watermarks.

Revision ID: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"

CDC_TABLES = [
    "billing.customers", "billing.subscriptions", "billing.debits", "billing.payments", "billing.mandates",
    "ops.contacts", "ops.cases", "experiments.assignments", "experiments.exposures", "experiments.outcomes",
    "ai.predictions", "ai.labels", "events.outbox",
]


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION core.stamp_cdc_txid() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.cdc_txid := pg_current_xact_id();
          RETURN NEW;
        END $$;
    """)
    for table in CDC_TABLES:
        name = table.replace(".", "_")
        op.execute(f"""
            ALTER TABLE {table} ADD COLUMN cdc_txid xid8;
            CREATE INDEX {name}_cdc ON {table} (tenant_id, cdc_txid);
            CREATE TRIGGER {name}_cdc BEFORE INSERT OR UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION core.stamp_cdc_txid();
        """)
    op.execute("""
        -- existing rows: stamp once so the first extraction sees them (xid8 of this migration)
        """ + "\n".join(f"UPDATE {t} SET cdc_txid = pg_current_xact_id() WHERE cdc_txid IS NULL;"
                        for t in CDC_TABLES) + """

        CREATE TABLE ingest.extract_watermarks (
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          source_table text NOT NULL,
          watermark    xid8 NOT NULL,
          rows_total   bigint NOT NULL DEFAULT 0,
          updated_at   timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, source_table)
        );

        -- Provider backfill progress: a window is recorded only after its rows are safely in bronze.
        CREATE TABLE ingest.backfill_checkpoints (
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          provider     text NOT NULL,
          entity       text NOT NULL,
          window_start timestamptz NOT NULL,
          window_end   timestamptz NOT NULL,
          rows         int NOT NULL,
          completed_at timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, provider, entity, window_start)
        );

        CREATE TABLE ingest.pipeline_runs (
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          run_id       text NOT NULL,
          trigger      text NOT NULL,          -- manual | schedule | onboarding | test
          status       text NOT NULL CHECK (status IN ('running','succeeded','failed')),
          steps        jsonb NOT NULL DEFAULT '[]'::jsonb,
          error        text,
          started_at   timestamptz NOT NULL,
          finished_at  timestamptz,
          PRIMARY KEY (tenant_id, run_id)
        );

        CREATE TABLE ai.data_health (
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          computed_at  timestamptz NOT NULL,
          run_id       text NOT NULL,
          report       jsonb NOT NULL,
          PRIMARY KEY (tenant_id, computed_at)
        );
    """)
    for table in ("ingest.extract_watermarks", "ingest.backfill_checkpoints", "ingest.pipeline_runs",
                  "ai.data_health"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.data_health, ingest.pipeline_runs, ingest.backfill_checkpoints, "
               "ingest.extract_watermarks;")
    for table in CDC_TABLES:
        name = table.replace(".", "_")
        op.execute(f"DROP TRIGGER IF EXISTS {name}_cdc ON {table}; ALTER TABLE {table} DROP COLUMN IF EXISTS cdc_txid;")
    op.execute("DROP FUNCTION IF EXISTS core.stamp_cdc_txid();")
