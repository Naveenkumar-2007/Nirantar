"""Conversation bodies for the inbox (P8.3, ADR-0020): message text encrypted with the tenant key.

The message ledger kept only a hash of each body. The Conversations inbox needs the words, so they are stored
AES-GCM encrypted with the business's data key (core.crypto) and decrypted only for signed-in members with read
access. `via` records how an inbound message arrived (text / audio → transcript / image / document).

Revision ID: 0022
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE comms.messages
          ADD COLUMN body_enc bytea,
          ADD COLUMN evidence_id text;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE comms.messages DROP COLUMN body_enc, DROP COLUMN evidence_id;")
