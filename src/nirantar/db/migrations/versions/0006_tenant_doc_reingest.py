"""Tenants may replace (delete + re-insert) their OWN documents/chunks on re-ingestion.
The shared ten_global corpus stays read-only for tenants.

Revision ID: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CURRENT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    for table in ("ai.documents", "ai.chunks"):
        op.execute(f"CREATE POLICY tenant_delete ON {table} FOR DELETE USING (tenant_id = {CURRENT});")
        op.execute(f"GRANT DELETE ON {table} TO nirantar_app;")


def downgrade() -> None:
    for table in ("ai.documents", "ai.chunks"):
        op.execute(f"REVOKE DELETE ON {table} FROM nirantar_app;")
        op.execute(f"DROP POLICY IF EXISTS tenant_delete ON {table};")
