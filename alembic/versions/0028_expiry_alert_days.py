"""Expiry alert days (plan 2026-11-27, P7): how many days before expiry a product's batches
show as close to expiry. Set in SRIBEES Online (product, else sub-category, else category,
else 30) and copied here by the catalog sync.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE online_products ADD COLUMN expiry_alert_days smallint "
               "CHECK (expiry_alert_days BETWEEN 1 AND 365)")


def downgrade() -> None:
    op.execute("ALTER TABLE online_products DROP COLUMN expiry_alert_days")
