"""Quick buy (owner 2026-11-26): a market cash buy is ordered and received in one step;
such an order may be received while Finance's approval is still coming.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supply_orders ADD COLUMN is_quick_buy boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE supply_orders DROP COLUMN is_quick_buy")
