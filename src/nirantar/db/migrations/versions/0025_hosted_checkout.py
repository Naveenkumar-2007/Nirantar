"""Hosted checkout (P8.6, ADR-0022): a payment request is either a provider payment link or a Nirantar pay page
backed by a provider order (Razorpay Orders + Checkout): branded, in the customer's language, no link quota, and
confirmed by the provider's payment signature.

Revision ID: 0025
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE billing.payment_requests
          ADD COLUMN kind text NOT NULL DEFAULT 'link' CHECK (kind IN ('link','checkout'));
        COMMENT ON COLUMN billing.payment_requests.provider_link_id IS
          'kind=link: provider payment link id; kind=checkout: provider order id';
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE billing.payment_requests DROP COLUMN kind;")
