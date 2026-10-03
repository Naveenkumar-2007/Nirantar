"""WhatsApp channel (P6, ADR-0017): message ledger, inbound routing, Meta template approval state.

comms.messages        every outbound/inbound message with its delivery status (tenant data, RLS)
comms.wa_routes       which tenant/customer a WhatsApp number belongs to — keyed by a peppered HMAC of the number,
                      never the number itself; used to route inbound messages before a tenant is known (platform)
comms.wa_message_ids  provider message id → tenant, to route delivery-status callbacks (platform)
config.whatsapp_templates  this deployment's Meta templates per registry key + language and their approval status

Revision ID: 0019
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE SCHEMA IF NOT EXISTS comms;
        GRANT USAGE ON SCHEMA comms TO nirantar_app;

        CREATE TABLE comms.messages (
          tenant_id     text NOT NULL,
          message_id    text NOT NULL,
          customer_id   text,
          channel       text NOT NULL CHECK (channel IN ('whatsapp','sms','email','voice')),
          direction     text NOT NULL CHECK (direction IN ('outbound','inbound')),
          kind          text NOT NULL,                 -- text | template | audio | image | document | button
          template_ref  text,
          status        text NOT NULL CHECK (status IN ('sent','delivered','read','failed','received')),
          error_code    text,
          error_title   text,
          body_sha256   text,
          created_at    timestamptz NOT NULL DEFAULT now(),
          updated_at    timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, message_id)
        );
        CREATE INDEX messages_customer_idx ON comms.messages (tenant_id, customer_id, created_at DESC);
        ALTER TABLE comms.messages ENABLE ROW LEVEL SECURITY;
        ALTER TABLE comms.messages FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON comms.messages USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON comms.messages TO nirantar_app;

        CREATE TABLE comms.wa_routes (
          phone_hash        text NOT NULL,
          tenant_id         text NOT NULL,
          customer_id       text NOT NULL,
          last_outbound_at  timestamptz,
          last_inbound_at   timestamptz,
          PRIMARY KEY (phone_hash, tenant_id)
        );
        CREATE TABLE comms.wa_message_ids (
          message_id  text PRIMARY KEY,
          tenant_id   text NOT NULL,
          created_at  timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT, UPDATE ON comms.wa_routes, comms.wa_message_ids TO nirantar_app;

        CREATE TABLE config.whatsapp_templates (
          template_key   text NOT NULL,           -- registry key, e.g. whatsapp.recovery
          language       text NOT NULL,           -- registry language: en | hi | te
          meta_name      text NOT NULL,
          meta_language  text NOT NULL,
          category       text NOT NULL CHECK (category IN ('UTILITY','MARKETING','AUTHENTICATION')),
          params         text[] NOT NULL,         -- registry placeholders in Meta's {{{{1}}}}..{{{{n}}}} order
          status         text NOT NULL,           -- PENDING | APPROVED | REJECTED | PAUSED | DISABLED
          meta_id        text,
          reason         text,
          submitted_at   timestamptz NOT NULL DEFAULT now(),
          updated_at     timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (template_key, language)
        );
        GRANT SELECT, INSERT, UPDATE ON config.whatsapp_templates TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS config.whatsapp_templates, comms.wa_message_ids, comms.wa_routes, comms.messages;
        DROP SCHEMA IF EXISTS comms;
    """)
