"""Orders in our own product names (owner 2026-11-26): an order line names an Online pack
size, units and a price; a supplier's offer is no longer needed (old lines keep theirs).
The base price is the bill price at each receipt, so one line may have several.

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supply_order_lines ALTER COLUMN offer_id DROP NOT NULL")
    op.execute("ALTER TABLE supply_order_lines ADD CONSTRAINT supply_order_lines_names_item "
               "CHECK (offer_id IS NOT NULL OR online_variant_id IS NOT NULL)")
    op.execute("CREATE UNIQUE INDEX uq_order_line_variant ON supply_order_lines "
               "(order_id, online_variant_id) WHERE offer_id IS NULL")
    # the base price comes from each bill now: a line received in two deliveries has two
    op.execute("ALTER TABLE base_prices DROP CONSTRAINT base_prices_line_id_key")


def downgrade() -> None:
    op.execute("ALTER TABLE base_prices ADD CONSTRAINT base_prices_line_id_key UNIQUE (line_id)")
    op.execute("DROP INDEX uq_order_line_variant")
    op.execute("ALTER TABLE supply_order_lines DROP CONSTRAINT supply_order_lines_names_item")
    op.execute("ALTER TABLE supply_order_lines ALTER COLUMN offer_id SET NOT NULL")
