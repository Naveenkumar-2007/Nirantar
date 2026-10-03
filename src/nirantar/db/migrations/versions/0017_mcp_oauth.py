"""OAuth 2.1 for the MCP server (P7, ADR-0016): registered clients, pending authorization requests, codes, tokens.

Clients and pending requests exist before a tenant is chosen (dynamic client registration; the merchant picks the
tenant by approving in their dashboard), so they are platform tables holding no tenant data. Codes and tokens are
tenant data under RLS; their plaintext carries the tenant id (like API keys) and only an HMAC hash is stored.

Revision ID: 0017
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE core.oauth_clients (
          client_id     text PRIMARY KEY,
          info          jsonb NOT NULL,          -- RFC 7591 client information (public metadata)
          created_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE core.oauth_requests (
          request_id    text PRIMARY KEY,
          client_id     text NOT NULL REFERENCES core.oauth_clients(client_id),
          params        jsonb NOT NULL,          -- scopes, redirect_uri, state, PKCE challenge, resource
          created_at    timestamptz NOT NULL DEFAULT now(),
          expires_at    timestamptz NOT NULL,
          decided_at    timestamptz,
          decision      text CHECK (decision IN ('approved','denied'))
        );
        GRANT SELECT, INSERT ON core.oauth_clients TO nirantar_app;
        GRANT SELECT, INSERT, UPDATE ON core.oauth_requests TO nirantar_app;

        CREATE TABLE core.oauth_grants (
          tenant_id     text NOT NULL,
          grant_id      text NOT NULL,
          client_id     text NOT NULL REFERENCES core.oauth_clients(client_id),
          principal_id  text NOT NULL,           -- who approved it
          scopes        text[] NOT NULL,
          resource      text,
          created_at    timestamptz NOT NULL DEFAULT now(),
          revoked_at    timestamptz,
          revoked_reason text,
          PRIMARY KEY (tenant_id, grant_id)
        );
        CREATE TABLE core.oauth_secrets (
          tenant_id     text NOT NULL,
          secret_hash   text NOT NULL,
          kind          text NOT NULL CHECK (kind IN ('code','access','refresh')),
          grant_id      text NOT NULL,
          params        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- code: PKCE challenge + redirect_uri
          expires_at    timestamptz NOT NULL,
          used_at       timestamptz,           -- codes and refresh tokens are single use
          created_at    timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, secret_hash),
          FOREIGN KEY (tenant_id, grant_id) REFERENCES core.oauth_grants(tenant_id, grant_id)
        );
    """)
    for table in ("core.oauth_grants", "core.oauth_secrets"):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.oauth_secrets, core.oauth_grants, core.oauth_requests, core.oauth_clients;")
