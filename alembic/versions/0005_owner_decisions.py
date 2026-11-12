"""Owner's decisions of 2026-11-12 evening (QUESTIONS.md answers 8, 10, 14).

- supplier_offers.held_reason: a price line the system holds back (e.g. our price above
  the selling price). Held lines are kept, shown to staff with a label, left out of the
  shared message, and cannot be ordered, until the supplier fixes them.
- supply_order_lines.online_product_id may be empty: an offer with no Online match can
  be ordered; its base price is written when it is matched at receiving.
- supply_orders.cancel_note: staff-only note next to the reason the supplier sees.

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE supplier_offers
            ADD COLUMN held_reason varchar(30)
                CHECK (held_reason IS NULL OR held_reason IN ('price_above_mrp'));
        CREATE INDEX ix_supplier_offers_held ON supplier_offers (supplier_id)
            WHERE held_reason IS NOT NULL;

        ALTER TABLE supply_order_lines ALTER COLUMN online_product_id DROP NOT NULL;
        ALTER TABLE supply_order_lines
            ADD CONSTRAINT ck_line_variant_needs_product
                CHECK (online_variant_id IS NULL OR online_product_id IS NOT NULL);

        ALTER TABLE supply_orders ADD COLUMN cancel_note text;
        """
    )


def downgrade() -> None:
    raise NotImplementedError
