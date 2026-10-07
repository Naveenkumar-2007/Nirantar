"""Recovery batches (P8.5, ADR-0021): an operator-launched, bounded recovery run with its own randomised holdout.

ops.recovery_batches       one run: who launched it, the experiment that measures it, its window and state
ops.recovery_batch_items   every selected item, its arm (treatment | holdout), the planned action, what happened
                           (executed / denied / pending approval / failed / skipped) and the verified outcome

Revision ID: 0023
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ops.recovery_batches (
          tenant_id      text NOT NULL,
          batch_id       text NOT NULL,
          name           text NOT NULL,
          experiment_id  text NOT NULL,
          holdout_bp     int NOT NULL CHECK (holdout_bp BETWEEN 0 AND 5000),
          window_days    int NOT NULL CHECK (window_days BETWEEN 1 AND 30),
          status         text NOT NULL CHECK (status IN ('running','stopping','measuring','completed','stopped')),
          launched_by    text NOT NULL,
          launched_at    timestamptz NOT NULL,
          ends_at        timestamptz NOT NULL,
          stopped_by     text,
          stopped_at     timestamptz,
          workflow_id    text,
          summary        jsonb NOT NULL DEFAULT '{}'::jsonb,
          PRIMARY KEY (tenant_id, batch_id)
        );
        CREATE TABLE ops.recovery_batch_items (
          tenant_id      text NOT NULL,
          batch_id       text NOT NULL,
          item_id        text NOT NULL,                 -- the queue item id, e.g. failed_debit:deb_…
          kind           text NOT NULL,
          subject_id     text NOT NULL,
          customer_id    text NOT NULL,
          amount_minor   bigint NOT NULL,
          arm            text NOT NULL CHECK (arm IN ('treatment','holdout')),
          action         text NOT NULL,
          state          text NOT NULL DEFAULT 'planned' CHECK (state IN
                           ('planned','held_out','executed','denied','pending_approval','failed','skipped','stopped')),
          detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
          action_ids     text[] NOT NULL DEFAULT '{}',
          outcome        text CHECK (outcome IN ('recovered','not_recovered')),
          value_minor    bigint NOT NULL DEFAULT 0,
          updated_at     timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, batch_id, item_id)
        );
        CREATE INDEX recovery_items_subject_idx ON ops.recovery_batch_items (tenant_id, subject_id);
    """)
    for t in ("ops.recovery_batches", "ops.recovery_batch_items"):
        op.execute(f"""
            ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {t} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {t} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.recovery_batch_items, ops.recovery_batches;")
