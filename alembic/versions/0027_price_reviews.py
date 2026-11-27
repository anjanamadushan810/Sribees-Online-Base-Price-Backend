"""Price reviews (plan 2026-11-27, P6): when a delivery reaches SRIBEES Online as a new batch
whose price differs from today's (a new MRP: same discount, worked out by Online), or whose
selling price is below what we paid, a row waits on the branch manager's dashboard: photo,
name, what we paid, the old and new MRP and selling price. They submit it as it is or edited;
Online's batch then sells at that price. Sales never wait for the review.

Revision ID: 0027
Revises: 0026
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE price_reviews (
            receipt_line_id   uuid PRIMARY KEY REFERENCES receipt_lines(receipt_line_id),
            branch_id         uuid NOT NULL REFERENCES branches(branch_id),
            online_batch_id   uuid NOT NULL,
            online_product_id uuid NOT NULL,
            online_variant_id uuid NOT NULL,
            base_price        numeric(14, 4),
            old_listed_price  numeric(14, 2),
            old_selling_price numeric(14, 2),
            listed_price      numeric(14, 2) NOT NULL,
            selling_price     numeric(14, 2) NOT NULL,
            status            varchar(10) NOT NULL DEFAULT 'waiting'
                CHECK (status IN ('waiting', 'kept', 'changed', 'failed')),
            final_price       numeric(14, 2),
            reason            varchar(300),
            reviewed_by       uuid REFERENCES users(user_id),
            reviewed_at       timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_price_reviews_waiting ON price_reviews(branch_id) "
               "WHERE status IN ('waiting', 'failed')")


def downgrade() -> None:
    op.execute("DROP TABLE price_reviews")
