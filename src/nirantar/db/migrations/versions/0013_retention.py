"""Retention & revival (P4, ADR-0014): churn-prior fits, win-back offers, `retention` settings namespace.

Revision ID: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE config.settings_versions DROP CONSTRAINT settings_versions_namespace_check;
        ALTER TABLE config.settings_versions ADD CONSTRAINT settings_versions_namespace_check
          CHECK (namespace IN ('channels','strategy','effects','experiments','policy','retention'));

        -- sBG (tenure) and churn-type rule fitted on the tenant's lifecycle: cold-start priors for M6/M13 and CLV
        CREATE TABLE ai.retention_fits (
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          fit_id      text NOT NULL,
          fitted_at   timestamptz NOT NULL,
          sbg         jsonb,                    -- {"alpha","beta"} or null when there is too little churn
          type_rule   jsonb,                    -- {"p_trouble","p_clean"} or null
          report      jsonb NOT NULL,           -- fit diagnostics (KM vs fitted), CLV totals
          PRIMARY KEY (tenant_id, fit_id)
        );

        -- A win-back offer. The offered amount is computed by code from the subscription amount and the arm —
        -- never by an LLM — and is what the reactivation payment link charges.
        CREATE TABLE billing.offers (
          tenant_id          text NOT NULL REFERENCES core.tenants(tenant_id),
          offer_id           text NOT NULL,
          case_id            text NOT NULL,
          experiment_id      text NOT NULL,
          subscription_id    text NOT NULL,
          customer_id        text NOT NULL,
          arm                text NOT NULL,
          kind               text NOT NULL CHECK (kind IN ('reminder','discount_pct')),
          discount_pct       int NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 90),
          list_amount_minor  bigint NOT NULL CHECK (list_amount_minor > 0),
          offer_amount_minor bigint NOT NULL CHECK (offer_amount_minor > 0 AND offer_amount_minor <= list_amount_minor),
          link_ref           text,
          status             text NOT NULL CHECK (status IN ('created','sent','redeemed','expired','withheld','denied')),
          created_at         timestamptz NOT NULL,
          expires_at         timestamptz NOT NULL,
          redeemed_payment_id text,
          PRIMARY KEY (tenant_id, offer_id),
          UNIQUE (tenant_id, case_id)
        );
    """)
    for table, grants in (("ai.retention_fits", "SELECT, INSERT"), ("billing.offers", "SELECT, INSERT, UPDATE")):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT {grants} ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS billing.offers, ai.retention_fits;
        ALTER TABLE config.settings_versions DROP CONSTRAINT settings_versions_namespace_check;
        ALTER TABLE config.settings_versions ADD CONSTRAINT settings_versions_namespace_check
          CHECK (namespace IN ('channels','strategy','effects','experiments','policy'));
    """)
