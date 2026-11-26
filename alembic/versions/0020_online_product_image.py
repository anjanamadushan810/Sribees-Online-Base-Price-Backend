"""Online product photo (owner 2026-11-26): the matching screen shows the product's main
photo, mirrored from Online's catalogue.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE online_products ADD COLUMN image_url varchar(500)")


def downgrade() -> None:
    op.execute("ALTER TABLE online_products DROP COLUMN image_url")
