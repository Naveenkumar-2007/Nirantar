"""Checkout drop-off recovery (ADR-0028): sessions the business's checkout reports, their events, and the bounded
recovery ladder run for each abandoned or failed one.

billing.checkout_sessions   one row per checkout (the business's own reference); cart value, how far the customer got,
                            failed attempts, the diagnosed cause, the experiment arm, and how it ended
ops.checkout_events         every reported or provider-observed event, idempotent on the business's event id
ops.checkout_chases         every recovery step: what was sent, the policy decision, when
billing.payment_requests    + checkout_session_id — a recovery link for a checkout (exactly one subject per request)

Revision ID: 0032
"""

from __future__ import annotations

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing.checkout_sessions (
          tenant_id          text NOT NULL,
          session_id         text NOT NULL,
          checkout_ref       text NOT NULL,                     -- the business's own checkout / order id
          customer_id        text,
          amount_minor       bigint NOT NULL CHECK (amount_minor > 0),
          currency           text NOT NULL DEFAULT 'INR',
          items              jsonb NOT NULL DEFAULT '[]'::jsonb,
          stage              text NOT NULL CHECK (stage IN ('initiated','payment_page')),
          status             text NOT NULL CHECK (status IN ('open','paid','expired','stopped')),
          attempts           integer NOT NULL DEFAULT 0,
          last_failure_code  text,
          cause              text,                              -- diagnosed when recovery starts
          arm                text CHECK (arm IN ('treatment','holdout')),
          experiment_id      text,
          provider           text,
          provider_order_id  text,
          first_contact_at   timestamptz,
          paid_at            timestamptz,
          paid_minor         bigint,
          paid_via           text CHECK (paid_via IN ('original','recovery_link')),
          stop_reason        text,
          created_at         timestamptz NOT NULL,
          last_activity_at   timestamptz NOT NULL,
          updated_at         timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, session_id),
          UNIQUE (tenant_id, checkout_ref),
          FOREIGN KEY (tenant_id, customer_id) REFERENCES billing.customers(tenant_id, customer_id)
        );
        CREATE INDEX checkout_open_idx ON billing.checkout_sessions (tenant_id, last_activity_at) WHERE status = 'open';
        CREATE INDEX checkout_created_idx ON billing.checkout_sessions (tenant_id, created_at DESC);
        CREATE UNIQUE INDEX checkout_order_idx ON billing.checkout_sessions (tenant_id, provider, provider_order_id)
          WHERE provider_order_id IS NOT NULL;

        CREATE TABLE ops.checkout_events (
          tenant_id     text NOT NULL,
          event_id      text NOT NULL,                         -- the business's id (or provider:<payment id>)
          session_id    text NOT NULL,
          type          text NOT NULL CHECK (type IN ('initiated','payment_page','payment_failed','paid','expired')),
          at            timestamptz NOT NULL,
          payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
          received_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, event_id),
          FOREIGN KEY (tenant_id, session_id) REFERENCES billing.checkout_sessions(tenant_id, session_id)
        );
        CREATE INDEX checkout_events_session_idx ON ops.checkout_events (tenant_id, session_id, at);

        CREATE TABLE ops.checkout_chases (
          tenant_id     text NOT NULL,
          session_id    text NOT NULL,
          step          text NOT NULL,                         -- nudge | follow_up | escalate
          status        text NOT NULL,                         -- executed | denied | skipped | failed | held_out
          action_id     text,
          detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at    timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, session_id, step)
        );

        ALTER TABLE billing.payment_requests ADD COLUMN checkout_session_id text,
          DROP CONSTRAINT payment_requests_subject,
          ADD CONSTRAINT payment_requests_subject
            CHECK (num_nonnulls(debit_id, invoice_id, invoice_ids, checkout_session_id) = 1);
        ALTER TABLE billing.payments ADD COLUMN checkout_session_id text;
    """)
    for t in ("billing.checkout_sessions", "ops.checkout_events", "ops.checkout_chases"):
        op.execute(f"""
            ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {t} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {t} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM billing.payment_requests WHERE checkout_session_id IS NOT NULL;
        ALTER TABLE billing.payment_requests DROP CONSTRAINT payment_requests_subject, DROP COLUMN checkout_session_id,
          ADD CONSTRAINT payment_requests_subject CHECK (num_nonnulls(debit_id, invoice_id, invoice_ids) = 1);
        ALTER TABLE billing.payments DROP COLUMN checkout_session_id;
        DROP TABLE IF EXISTS ops.checkout_chases, ops.checkout_events, billing.checkout_sessions;
    """)
