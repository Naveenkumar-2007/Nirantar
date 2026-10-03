"""People sign in with OIDC (P8, ADR-0018): which businesses (tenants) a signed-in user belongs to, with roles.

Looked up BEFORE a tenant is chosen (a user may belong to several), so these are platform tables holding only the
identity provider's subject id, display facts and tenant ids — no tenant business data.

Revision ID: 0020
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE core.users (
          user_sub     text PRIMARY KEY,           -- OIDC `sub` from the issuer
          email        text,
          name         text,
          created_at   timestamptz NOT NULL DEFAULT now(),
          last_seen_at timestamptz
        );
        CREATE TABLE core.user_memberships (
          user_sub    text NOT NULL REFERENCES core.users(user_sub),
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          roles       text[] NOT NULL,
          status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
          invited_by  text,
          created_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (user_sub, tenant_id)
        );
        CREATE INDEX user_memberships_tenant_idx ON core.user_memberships (tenant_id);
        GRANT SELECT, INSERT, UPDATE ON core.users, core.user_memberships TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.user_memberships, core.users;")
