"""Receipt drafts (plan 2026-11-27, P2): staff receive a delivery product by product in the
staff app; every product is saved on the server at once, so a dead phone loses nothing.
"Finish" turns the draft into the real receipt. Each scanned QR label is reserved by the
draft at once: the primary key on the number means one QR can never sit in two drafts (two
phones) or on two products.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE receipt_drafts (
            draft_id    uuid PRIMARY KEY,
            order_id    uuid NOT NULL REFERENCES supply_orders(order_id),
            status      varchar(10) NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'finished', 'discarded')),
            receipt_id  uuid REFERENCES receipts(receipt_id),
            created_by  uuid REFERENCES users(user_id),
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            closed_by   uuid REFERENCES users(user_id),
            closed_at   timestamptz
        )""")
    # one open draft per order: two phones on one delivery share it
    op.execute("CREATE UNIQUE INDEX ux_receipt_drafts_open ON receipt_drafts(order_id) "
               "WHERE status = 'open'")
    op.execute("""
        CREATE TABLE receipt_draft_lines (
            draft_line_id     uuid PRIMARY KEY,
            draft_id          uuid NOT NULL REFERENCES receipt_drafts(draft_id),
            order_line_id     uuid REFERENCES supply_order_lines(line_id),
            online_variant_id uuid REFERENCES online_variants(online_variant_id),
            barcode           varchar(14),
            units             integer NOT NULL CHECK (units > 0),
            free_units        integer NOT NULL DEFAULT 0 CHECK (free_units >= 0),
            weight_kg         numeric(14, 3) CHECK (weight_kg > 0),
            expiry_date       date NOT NULL,
            mfg_date          date,
            mrp               numeric(14, 2) CHECK (mrp >= 0),
            unit_price        numeric(14, 4) CHECK (unit_price >= 0),
            listed_price      numeric(14, 2),
            selling_price     numeric(14, 2),
            created_by        uuid REFERENCES users(user_id),
            created_at        timestamptz NOT NULL DEFAULT now(),
            CHECK (order_line_id IS NOT NULL OR online_variant_id IS NOT NULL)
        )""")
    op.execute("CREATE INDEX ix_receipt_draft_lines_draft ON receipt_draft_lines(draft_id)")
    op.execute("""
        CREATE TABLE receipt_draft_labels (
            number        varchar(10) PRIMARY KEY REFERENCES labels(number),
            draft_id      uuid NOT NULL REFERENCES receipt_drafts(draft_id),
            draft_line_id uuid REFERENCES receipt_draft_lines(draft_line_id) ON DELETE SET NULL,
            reserved_by   uuid REFERENCES users(user_id),
            reserved_at   timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_receipt_draft_labels_line ON receipt_draft_labels(draft_line_id)")


def downgrade() -> None:
    op.execute("DROP TABLE receipt_draft_labels")
    op.execute("DROP TABLE receipt_draft_lines")
    op.execute("DROP TABLE receipt_drafts")
