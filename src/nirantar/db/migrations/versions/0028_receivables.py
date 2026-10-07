"""B2B receivables (P10 front, ADR-0025): invoices, partial payments, and a polite escalation ladder.

billing.invoices            one invoice to a business customer; paid_minor grows with verified payments
billing.payment_requests    + invoice_id (a link / pay page can collect an invoice instead of a debit)
billing.payments            + invoice_id (a verified payment applied to an invoice)
ops.invoice_chases          every ladder step: what was sent, the policy decision, when

Revision ID: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing.invoices (
          tenant_id        text NOT NULL,
          invoice_id       text NOT NULL,
          customer_id      text NOT NULL,
          number           text NOT NULL,                 -- the business's own invoice number
          description      text,
          issued_on        date NOT NULL,
          due_on           date NOT NULL,
          amount_minor     bigint NOT NULL CHECK (amount_minor > 0),
          paid_minor       bigint NOT NULL DEFAULT 0 CHECK (paid_minor >= 0),
          currency         text NOT NULL DEFAULT 'INR',
          status           text NOT NULL CHECK (status IN
                             ('open','partially_paid','paid','disputed','written_off','cancelled')),
          dispute_reason   text,
          created_by       text NOT NULL,
          created_at       timestamptz NOT NULL DEFAULT now(),
          updated_at       timestamptz NOT NULL DEFAULT now(),
          closed_at        timestamptz,
          PRIMARY KEY (tenant_id, invoice_id),
          UNIQUE (tenant_id, number),
          FOREIGN KEY (tenant_id, customer_id) REFERENCES billing.customers(tenant_id, customer_id),
          CHECK (due_on >= issued_on)
        );
        CREATE INDEX invoices_open_idx ON billing.invoices (tenant_id, due_on) WHERE status IN ('open','partially_paid');

        ALTER TABLE billing.payment_requests ALTER COLUMN debit_id DROP NOT NULL,
          ADD COLUMN invoice_id text,
          ADD CONSTRAINT payment_requests_subject CHECK (num_nonnulls(debit_id, invoice_id) = 1);
        ALTER TABLE billing.payments ADD COLUMN invoice_id text;

        CREATE TABLE ops.invoice_chases (
          tenant_id     text NOT NULL,
          invoice_id    text NOT NULL,
          step          text NOT NULL,                     -- reminder | due | overdue_1 | overdue_2 | final | human
          status        text NOT NULL,                     -- executed | denied | pending_approval | failed | skipped
          action_id     text,
          detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at    timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, invoice_id, step)
        );
    """)
    for t in ("billing.invoices", "ops.invoice_chases"):
        op.execute(f"""
            ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {t} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {t} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS ops.invoice_chases, billing.invoices;
        ALTER TABLE billing.payments DROP COLUMN invoice_id;
        ALTER TABLE billing.payment_requests DROP CONSTRAINT payment_requests_subject, DROP COLUMN invoice_id;
    """)
