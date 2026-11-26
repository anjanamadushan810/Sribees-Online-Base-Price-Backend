"""Receiving with a transport charge and Online stock (owner 2026-11-26): the charge is
split over the received lines by the Online product's weight, the base price is the
delivered price (the bill price is kept beside it), and each received line is sent to
SRIBEESonline once.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE online_products ADD COLUMN weight_grams integer "
               "CHECK (weight_grams > 0)")
    # NULL on rows from before this change: there the base price is the bill price
    op.execute("ALTER TABLE base_prices ADD COLUMN bill_price numeric(12,2) "
               "CHECK (bill_price >= 0)")
    op.execute("ALTER TABLE receipts ADD COLUMN transport_charge numeric(12,2) NOT NULL "
               "DEFAULT 0 CHECK (transport_charge >= 0)")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN listed_price numeric(12,2) "
               "CHECK (listed_price >= 0), ADD COLUMN selling_price numeric(12,2) "
               "CHECK (selling_price >= 0)")
    op.execute("""
        CREATE TABLE online_stock_sends (
            receipt_line_id uuid PRIMARY KEY REFERENCES receipt_lines(receipt_line_id),
            receipt_id      uuid NOT NULL REFERENCES receipts(receipt_id),
            status          varchar(10) NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'sent', 'refused')),
            reason          varchar(300),
            attempts        integer NOT NULL DEFAULT 0,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_online_stock_sends_open ON online_stock_sends (receipt_id) "
               "WHERE status <> 'sent'")


def downgrade() -> None:
    op.execute("DROP TABLE online_stock_sends")
    op.execute("ALTER TABLE receipt_lines DROP COLUMN listed_price, DROP COLUMN selling_price")
    op.execute("ALTER TABLE receipts DROP COLUMN transport_charge")
    op.execute("ALTER TABLE base_prices DROP COLUMN bill_price")
    op.execute("ALTER TABLE online_products DROP COLUMN weight_grams")
