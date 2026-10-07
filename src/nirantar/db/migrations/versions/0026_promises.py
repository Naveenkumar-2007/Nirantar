"""Promise-to-pay tracker (P10, ADR-0023): every promise a customer makes, and whether it was kept.

ops.promises   open → kept (verified payment by the end of the promised day) | broken (no payment) |
               superseded (a newer promise for the same debit). Kept/broken feed the customer's promise-keeping
               rate (Customer 360, recovery queue) and, later, the models.

Revision ID: 0026
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE ops.promises (
          tenant_id      text NOT NULL,
          promise_id     text NOT NULL,
          debit_id       text NOT NULL,
          customer_id    text NOT NULL,
          promised_date  date,
          source         text NOT NULL,          -- whatsapp_text | voice_note | operator
          quote          text,                   -- the customer's words, OTP-redacted, max 300 chars
          status         text NOT NULL CHECK (status IN ('open','kept','broken','superseded')),
          reminder_sent_at timestamptz,
          created_at     timestamptz NOT NULL,
          resolved_at    timestamptz,
          PRIMARY KEY (tenant_id, promise_id)
        );
        CREATE INDEX promises_debit_idx ON ops.promises (tenant_id, debit_id) WHERE status = 'open';
        CREATE INDEX promises_customer_idx ON ops.promises (tenant_id, customer_id);
        ALTER TABLE ops.promises ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ops.promises FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON ops.promises USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON ops.promises TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.promises;")
