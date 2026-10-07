"""Plans, enrollment and collection methods (P8.6, ADR-0022): Nirantar runs the billing schedule itself.

billing.plans             what a business sells: name, amount, interval, default collection method
billing.subscriptions     + plan_id, + collection_method:
                            provider_subscription  the provider charges (e.g. Razorpay Subscriptions)
                            mandate                Nirantar charges a registered UPI AutoPay / e-mandate token
                            payment_link           Nirantar sends a payment link on each due date (works everywhere)
billing.payment_requests  every payment link sent for a debit: provider link id, URL, status, the payment that paid it

Revision ID: 0024
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing.plans (
          tenant_id          text NOT NULL,
          plan_id            text NOT NULL,
          name               text NOT NULL,
          description        text,
          amount_minor       bigint NOT NULL CHECK (amount_minor > 0),
          currency           text NOT NULL DEFAULT 'INR',
          interval           text NOT NULL CHECK (interval IN ('weekly','monthly','quarterly','yearly')),
          collection_method  text NOT NULL CHECK (collection_method IN ('provider_subscription','mandate','payment_link')),
          active             boolean NOT NULL DEFAULT true,
          created_by         text NOT NULL,
          created_at         timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, plan_id)
        );
        ALTER TABLE billing.subscriptions
          ADD COLUMN plan_id text,
          ADD COLUMN collection_method text NOT NULL DEFAULT 'provider_subscription'
            CHECK (collection_method IN ('provider_subscription','mandate','payment_link'));
        CREATE TABLE billing.payment_requests (
          tenant_id            text NOT NULL,
          request_id           text NOT NULL,
          debit_id             text NOT NULL,
          customer_id          text NOT NULL,
          provider             text NOT NULL,
          provider_link_id     text NOT NULL,
          url                  text NOT NULL,
          amount_minor         bigint NOT NULL,
          status               text NOT NULL CHECK (status IN ('created','sent','paid','expired','cancelled')),
          channel_message_id   text,
          provider_payment_id  text,
          created_at           timestamptz NOT NULL,
          sent_at              timestamptz,
          paid_at              timestamptz,
          PRIMARY KEY (tenant_id, request_id),
          UNIQUE (tenant_id, provider, provider_link_id)
        );
        CREATE INDEX payment_requests_debit_idx ON billing.payment_requests (tenant_id, debit_id);
    """)
    for t in ("billing.plans", "billing.payment_requests"):
        op.execute(f"""
            ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {t} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {t} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS billing.payment_requests, billing.plans;
        ALTER TABLE billing.subscriptions DROP COLUMN collection_method, DROP COLUMN plan_id;
    """)
