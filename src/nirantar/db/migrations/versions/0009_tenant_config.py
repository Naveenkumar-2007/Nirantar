"""Per-tenant configuration: versioned settings, template registry, learned parameters (P1, ADR-0011).

Revision ID: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS config;

        -- Every change is a new version (full document), never an update: history = audit trail.
        CREATE TABLE config.settings_versions (
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          namespace   text NOT NULL CHECK (namespace IN ('channels','strategy','effects','experiments','policy')),
          version     int  NOT NULL CHECK (version >= 1),
          value       jsonb NOT NULL,
          changed_by  text NOT NULL,
          reason      text NOT NULL,
          created_at  timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, namespace, version)
        );

        CREATE TABLE config.templates (
          tenant_id     text NOT NULL REFERENCES core.tenants(tenant_id),
          template_key  text NOT NULL,
          language      text NOT NULL,
          version       int  NOT NULL CHECK (version >= 1),
          body          text NOT NULL,
          status        text NOT NULL CHECK (status IN ('pending','approved','rejected','retired')),
          created_by    text NOT NULL,
          created_at    timestamptz NOT NULL,
          decided_by    text,
          decided_at    timestamptz,
          checks        jsonb NOT NULL DEFAULT '{}'::jsonb,
          PRIMARY KEY (tenant_id, template_key, language, version),
          CHECK (decided_by IS NULL OR decided_by <> created_by)       -- maker-checker, enforced by the DB too
        );
        -- At most one live version per (key, language).
        CREATE UNIQUE INDEX templates_one_approved ON config.templates (tenant_id, template_key, language)
          WHERE status = 'approved';

        -- Only the review columns may change; the text of a template version is immutable.
        CREATE FUNCTION config.template_body_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.body <> OLD.body OR NEW.created_by <> OLD.created_by OR NEW.version <> OLD.version
             OR NEW.template_key <> OLD.template_key OR NEW.language <> OLD.language THEN
            RAISE EXCEPTION 'template versions are immutable; create a new version';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER templates_immutable BEFORE UPDATE ON config.templates
          FOR EACH ROW EXECUTE FUNCTION config.template_body_immutable();

        -- Estimates learned from the tenant's own verified outcomes, with the evidence that produced them.
        CREATE TABLE config.learned_params (
          tenant_id   text NOT NULL REFERENCES core.tenants(tenant_id),
          name        text NOT NULL,
          version     int  NOT NULL CHECK (version >= 1),
          value       jsonb NOT NULL,
          evidence    jsonb NOT NULL,
          created_at  timestamptz NOT NULL,
          PRIMARY KEY (tenant_id, name, version)
        );

        CREATE TRIGGER settings_append_only BEFORE UPDATE OR DELETE ON config.settings_versions
          FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
        CREATE TRIGGER learned_append_only BEFORE UPDATE OR DELETE ON config.learned_params
          FOR EACH ROW EXECUTE FUNCTION core.reject_mutation();
        GRANT USAGE ON SCHEMA config TO nirantar_app;
        """
    )
    for table, grants in (("config.settings_versions", "SELECT, INSERT"), ("config.templates", "SELECT, INSERT, UPDATE"),
                          ("config.learned_params", "SELECT, INSERT")):
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table} USING (tenant_id = {CURRENT}) WITH CHECK (tenant_id = {CURRENT});
            GRANT {grants} ON {table} TO nirantar_app;
        """)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS config CASCADE;")
