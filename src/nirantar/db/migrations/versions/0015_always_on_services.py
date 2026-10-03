"""Always-on services (P5, ADR-0015): `operations` settings namespace and the event bridge's dead-letter table.

Revision ID: 0015
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE config.settings_versions DROP CONSTRAINT settings_versions_namespace_check;
        ALTER TABLE config.settings_versions ADD CONSTRAINT settings_versions_namespace_check
          CHECK (namespace IN ('channels','strategy','effects','experiments','policy','retention','operations'));

        -- Events a consumer could not handle after its retries. Kept per tenant (RLS) so the operator sees them
        -- in the product; the same envelope also goes to the Kafka DLQ topic. Replays are manual and audited.
        CREATE TABLE events.consumer_dead_letters (
          tenant_id    text NOT NULL,
          event_id     text NOT NULL,
          consumer     text NOT NULL,
          event_type   text NOT NULL,
          error        text NOT NULL,
          envelope     jsonb NOT NULL,
          attempts     int NOT NULL,
          created_at   timestamptz NOT NULL DEFAULT now(),
          replayed_at  timestamptz,
          PRIMARY KEY (tenant_id, consumer, event_id)
        );
        ALTER TABLE events.consumer_dead_letters ENABLE ROW LEVEL SECURITY;
        ALTER TABLE events.consumer_dead_letters FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON events.consumer_dead_letters
          USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON events.consumer_dead_letters TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS events.consumer_dead_letters;
        DELETE FROM config.settings_versions WHERE namespace='operations';
        ALTER TABLE config.settings_versions DROP CONSTRAINT settings_versions_namespace_check;
        ALTER TABLE config.settings_versions ADD CONSTRAINT settings_versions_namespace_check
          CHECK (namespace IN ('channels','strategy','effects','experiments','policy','retention'));
    """)
