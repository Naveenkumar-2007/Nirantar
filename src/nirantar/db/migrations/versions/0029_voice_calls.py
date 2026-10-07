"""Live voice calls (P11, ADR-0026): every outbound recovery call, its outcome and its (encrypted) transcript.

comms.calls   one Exotel call: who, why (debit), status from Exotel callbacks, what the caller said (redacted of OTPs,
              then AES-GCM encrypted with the business's key), the outcome (promise / hardship / opt-out / …) and the
              promise it produced.

Revision ID: 0029
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE comms.calls (
          tenant_id       text NOT NULL,
          call_id         text NOT NULL,
          provider        text NOT NULL DEFAULT 'exotel',
          call_sid        text,
          debit_id        text,
          customer_id     text NOT NULL,
          language        text NOT NULL,
          status          text NOT NULL CHECK (status IN
                            ('requested','ringing','in_progress','completed','no_answer','busy','failed','canceled')),
          outcome         text,                    -- promise_to_pay | hardship | opt_out | no_commitment | …
          transcript_enc  bytea,
          promise_id      text,
          duration_s      int,
          action_id       text,
          created_at      timestamptz NOT NULL,
          started_at      timestamptz,
          ended_at        timestamptz,
          PRIMARY KEY (tenant_id, call_id)
        );
        CREATE UNIQUE INDEX calls_sid_idx ON comms.calls (provider, call_sid) WHERE call_sid IS NOT NULL;
        ALTER TABLE comms.calls ENABLE ROW LEVEL SECURITY;
        ALTER TABLE comms.calls FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON comms.calls USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON comms.calls TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS comms.calls;")
