"""Landed cost (joint-go-live plan B2): the supplier's delivery charge is spread over the
goods received, by value, into each batch's cost. The base price stays the supplier's own
price (decision: base price = supplier price only).

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE receipt_lines ADD COLUMN landed_cost numeric(14,2) NOT NULL DEFAULT 0 "
               "CHECK (landed_cost >= 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE receipt_lines DROP COLUMN landed_cost")
