"""Identity: principals (users/services) and API keys, tenant-isolated.

Revision ID: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE core.principals (
          tenant_id     text NOT NULL REFERENCES core.tenants(tenant_id),
          principal_id  text NOT NULL,
          kind          text NOT NULL CHECK (kind IN ('user','service')),
          name          text NOT NULL,
          roles         text[] NOT NULL,
          status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
          created_at    timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, principal_id)
        );
        CREATE TABLE core.api_keys (
          tenant_id     text NOT NULL,
          key_id        text NOT NULL,
          principal_id  text NOT NULL,
          key_hash      text NOT NULL,          -- HMAC-SHA256(pepper, secret); secret never stored
          created_at    timestamptz NOT NULL DEFAULT now(),
          expires_at    timestamptz,
          last_used_at  timestamptz,
          revoked_at    timestamptz,
          PRIMARY KEY (tenant_id, key_id),
          FOREIGN KEY (tenant_id, principal_id) REFERENCES core.principals(tenant_id, principal_id)
        );
        """
    )
    for table in ("core.principals", "core.api_keys"):
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT SELECT, INSERT, UPDATE ON {table} TO nirantar_app;
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.api_keys; DROP TABLE IF EXISTS core.principals;")
