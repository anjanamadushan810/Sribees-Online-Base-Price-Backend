"""Goods that were not ordered (owner 2026-11-26): at receiving, staff may add products we
already sell on SRIBEES Online. Such a line joins the order as "added at receiving"; every
other line still changes only while the order is a draft.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supply_order_lines ADD COLUMN added_at_receiving boolean "
               "NOT NULL DEFAULT false")
    # the supplier's bill, photographed at receiving (owner plan 2026-11-26, P3)
    op.execute("""
        CREATE TABLE receipt_photos (
            receipt_id   uuid PRIMARY KEY REFERENCES receipts(receipt_id),
            content_type varchar(20) NOT NULL
                CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
            data         bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 3145728),
            uploaded_by  uuid REFERENCES users(user_id),
            uploaded_at  timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE OR REPLACE FUNCTION order_lines_only_in_draft() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE st varchar;
        BEGIN
            SELECT status INTO st FROM supply_orders
             WHERE order_id = coalesce(NEW.order_id, OLD.order_id);
            IF TG_OP = 'INSERT' AND NEW.added_at_receiving
               AND st IN ('submitted', 'approved', 'accepted') THEN
                RETURN NEW;  -- a product that came without being ordered
            END IF;
            IF st IS DISTINCT FROM 'draft' THEN
                RAISE EXCEPTION 'order lines can change only while the order is a draft'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN coalesce(NEW, OLD);
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION order_lines_only_in_draft() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE st varchar;
        BEGIN
            SELECT status INTO st FROM supply_orders
             WHERE order_id = coalesce(NEW.order_id, OLD.order_id);
            IF st IS DISTINCT FROM 'draft' THEN
                RAISE EXCEPTION 'order lines can change only while the order is a draft'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN coalesce(NEW, OLD);
        END $$;
    """)
    op.execute("DROP TABLE receipt_photos")
    op.execute("ALTER TABLE supply_order_lines DROP COLUMN added_at_receiving")
