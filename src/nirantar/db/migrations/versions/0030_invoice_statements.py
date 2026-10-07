"""Customer statements (ADR-0025 amendment): one message and one "pay all" link for all of a customer's open invoices.

billing.payment_requests     + invoice_ids text[] — a statement request; exactly one of debit_id / invoice_id /
                               invoice_ids is set
billing.payment_allocations  how a verified payment was split across invoices (oldest due first) — auditable,
                               idempotent per (provider payment, invoice)

Revision ID: 0030
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE billing.payment_requests DROP CONSTRAINT payment_requests_subject,
          ADD COLUMN invoice_ids text[],
          ADD CONSTRAINT payment_requests_subject CHECK (num_nonnulls(debit_id, invoice_id, invoice_ids) = 1);
        CREATE TABLE billing.payment_allocations (
          tenant_id            text NOT NULL,
          provider             text NOT NULL,
          provider_payment_id  text NOT NULL,
          invoice_id           text NOT NULL,
          amount_minor         bigint NOT NULL CHECK (amount_minor > 0),
          created_at           timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, provider, provider_payment_id, invoice_id)
        );
        ALTER TABLE billing.payment_allocations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE billing.payment_allocations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON billing.payment_allocations USING (tenant_id = {CURRENT})
          WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT ON billing.payment_allocations TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS billing.payment_allocations;
        ALTER TABLE billing.payment_requests DROP CONSTRAINT payment_requests_subject, DROP COLUMN invoice_ids,
          ADD CONSTRAINT payment_requests_subject CHECK (num_nonnulls(debit_id, invoice_id) = 1);
    """)
