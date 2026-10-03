"""Mandates from provider truth (P5, ADR-0015 §10): provider customer ref, failure reason, verification time, and
one row per provider token. Also: the app may reset `events.outbox.published_at` — the operator's dead-letter replay
(re-delivery only; every consumer is idempotent, and the replay is audited).

Revision ID: 0016
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE billing.mandates
          ADD COLUMN provider_customer_ref text,
          ADD COLUMN failure_reason text,
          ADD COLUMN last_verified_at timestamptz,
          ALTER COLUMN max_amount_minor DROP NOT NULL;   -- unknown until the provider reports it; never guessed
        CREATE UNIQUE INDEX mandates_provider_token_uq ON billing.mandates (tenant_id, provider, provider_token_id)
          WHERE provider_token_id IS NOT NULL;
        GRANT UPDATE ON billing.mandates TO nirantar_app;
        GRANT UPDATE (published_at) ON events.outbox TO nirantar_app;
    """)


def downgrade() -> None:
    op.execute("""
        REVOKE UPDATE (published_at) ON events.outbox FROM nirantar_app;
        DROP INDEX IF EXISTS billing.mandates_provider_token_uq;
        UPDATE billing.mandates SET max_amount_minor = 1 WHERE max_amount_minor IS NULL;
        ALTER TABLE billing.mandates ALTER COLUMN max_amount_minor SET NOT NULL;
        ALTER TABLE billing.mandates DROP COLUMN provider_customer_ref, DROP COLUMN failure_reason,
          DROP COLUMN last_verified_at;
    """)
