"""Payment health (P10 payment-degradation front, ADR-0024): detect issuer incidents, stop blaming customers, recover.

billing.payments.issuer     the bank / UPI PSP bank / wallet / card issuer a payment depended on
core.payment_incidents      PLATFORM-level, aggregates only (issuer, rail, rates, times) — no tenant or customer data;
                            detected across every business so a small business benefits from everyone's traffic
ops.incident_recoveries     per business: which failed debits an incident hit, and the recovery action taken

Revision ID: 0027
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE billing.payments ADD COLUMN issuer text;
        CREATE INDEX payments_health_idx ON billing.payments (method, issuer, provider_created_at)
          WHERE issuer IS NOT NULL;

        CREATE TABLE core.payment_incidents (
          incident_id     text PRIMARY KEY,
          rail            text NOT NULL,            -- upi | netbanking | card | emandate | wallet …
          issuer          text NOT NULL,
          started_at      timestamptz NOT NULL,      -- first hour of the new regime
          detected_at     timestamptz NOT NULL,
          ended_at        timestamptz,              -- first healthy hour after it
          status          text NOT NULL CHECK (status IN ('open','closed')),
          baseline_rate   double precision NOT NULL,
          peak_rate       double precision NOT NULL,
          attempts        bigint NOT NULL,
          failures        bigint NOT NULL,
          detector        text NOT NULL,            -- e.g. bocpd:v1
          recovery_done   boolean NOT NULL DEFAULT false
        );
        CREATE UNIQUE INDEX payment_incidents_open ON core.payment_incidents (rail, issuer) WHERE status = 'open';
        GRANT SELECT ON core.payment_incidents TO nirantar_app;

        CREATE TABLE ops.incident_recoveries (
          tenant_id     text NOT NULL,
          incident_id   text NOT NULL,
          debit_id      text NOT NULL,
          customer_id   text NOT NULL,
          action        text NOT NULL,              -- payment_request | skipped
          status        text NOT NULL,              -- executed | denied | failed | skipped
          detail        jsonb NOT NULL DEFAULT '{{}}'::jsonb,
          created_at    timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, incident_id, debit_id)
        );
        ALTER TABLE ops.incident_recoveries ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ops.incident_recoveries FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON ops.incident_recoveries USING (tenant_id = {CURRENT})
          WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON ops.incident_recoveries TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS ops.incident_recoveries, core.payment_incidents;
        DROP INDEX IF EXISTS billing.payments_health_idx;
        ALTER TABLE billing.payments DROP COLUMN issuer;
    """)
