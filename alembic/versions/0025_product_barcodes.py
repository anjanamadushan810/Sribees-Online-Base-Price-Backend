"""Product barcodes (owner plan 2026-11-27, barcode-first receiving P1): the EAN/UPC printed on
a pack points at one of our Online pack sizes. Linked by store staff the first time a barcode
is scanned; one pack size may have several barcodes (new packaging).

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE product_barcodes (
            barcode           varchar(14) PRIMARY KEY
                CHECK (barcode ~ '^[0-9]{8,14}$'),
            online_variant_id uuid NOT NULL REFERENCES online_variants(online_variant_id),
            created_by        uuid REFERENCES users(user_id),
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_by        uuid REFERENCES users(user_id),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_product_barcodes_variant ON product_barcodes(online_variant_id)")


def downgrade() -> None:
    op.execute("DROP TABLE product_barcodes")
