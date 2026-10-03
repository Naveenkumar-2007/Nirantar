"""Merchant-owned secrets and team invites (P8.2, ADR-0019).

core.tenant_secrets  a business's own credentials (e.g. its Razorpay keys), AES-GCM encrypted with the tenant's data
                     key (core.crypto); referenced as `tenant:<secret_id>`; tenant data under RLS
core.invites         an owner invites a person by email with a role; claimed on sign-in with a VERIFIED email
                     (platform table: looked up before a tenant is chosen; holds an email hash, never the address)

Revision ID: 0021
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE core.tenant_secrets (
          tenant_id    text NOT NULL,
          secret_id    text NOT NULL,
          purpose      text NOT NULL,                 -- e.g. razorpay.key_secret
          ciphertext   bytea NOT NULL,
          created_by   text NOT NULL,
          created_at   timestamptz NOT NULL DEFAULT now(),
          retired_at   timestamptz,
          PRIMARY KEY (tenant_id, secret_id)
        );
        ALTER TABLE core.tenant_secrets ENABLE ROW LEVEL SECURITY;
        ALTER TABLE core.tenant_secrets FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON core.tenant_secrets USING (tenant_id = {CURRENT})
          WITH CHECK (tenant_id = {CURRENT});
        GRANT SELECT, INSERT, UPDATE ON core.tenant_secrets TO nirantar_app;

        CREATE TABLE core.invites (
          invite_id    text PRIMARY KEY,
          tenant_id    text NOT NULL REFERENCES core.tenants(tenant_id),
          email_hash   text NOT NULL,                 -- peppered HMAC of the lower-cased address
          roles        text[] NOT NULL,
          invited_by   text NOT NULL,
          created_at   timestamptz NOT NULL DEFAULT now(),
          expires_at   timestamptz NOT NULL,
          accepted_by  text,
          accepted_at  timestamptz,
          revoked_at   timestamptz
        );
        CREATE INDEX invites_email_idx ON core.invites (email_hash) WHERE accepted_at IS NULL AND revoked_at IS NULL;
        GRANT SELECT, INSERT, UPDATE ON core.invites TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.invites, core.tenant_secrets;")
