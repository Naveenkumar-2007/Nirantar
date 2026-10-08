"""Mandate collection and the retry sequencer (ADR-0029): every charge attempt on a mandate debit, with its notice.

billing.debit_attempts   attempt 1 is the due-date charge; 2.. are retries. Each records when it was planned and why,
                         when the pre-debit notice went out (a charge is refused unless that was ≥24h earlier), the
                         provider payment, and the outcome. (tenant, debit, attempt) is unique, so a charge can never
                         be made twice for the same attempt.

Revision ID: 0033
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE billing.debit_attempts (
          tenant_id            text NOT NULL,
          debit_id             text NOT NULL,
          attempt              integer NOT NULL CHECK (attempt BETWEEN 1 AND 5),
          kind                 text NOT NULL CHECK (kind IN ('initial','retry')),
          planned_for          timestamptz NOT NULL,
          reason               text NOT NULL,
          failure_category     text,                       -- why the previous attempt failed (retries)
          notice_sent_at       timestamptz,
          notice_action_id     text,
          charged_at           timestamptz,
          charge_action_id     text,
          provider_payment_id  text,
          provider_order_id    text,
          status               text NOT NULL CHECK (status IN
                                 ('planned','notified','charging','captured','failed','cancelled')),
          created_at           timestamptz NOT NULL,
          updated_at           timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, debit_id, attempt),
          FOREIGN KEY (tenant_id, debit_id) REFERENCES billing.debits(tenant_id, debit_id)
        );
        CREATE INDEX debit_attempts_due_idx ON billing.debit_attempts (tenant_id, planned_for)
          WHERE status IN ('planned','notified');
        ALTER TABLE billing.debit_attempts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE billing.debit_attempts FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON billing.debit_attempts USING (tenant_id = {CURRENT})
          WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON billing.debit_attempts TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing.debit_attempts")
