"""Held stock (owner 2026-11-27): part of a delivery is checked (count, price, expiry) and kept
aside WITHOUT QR labels on busy days. It is in stock and in Finance from the day it arrives,
but not on SRIBEES Online. Later staff stick QR labels on some of it; that part (a release)
goes into the shop and Online's stock.

- receipt_lines.hold / receipt_draft_lines.hold: the part came in without labels
- stock_releases: labels stuck on held units, each one sent to Online once (keyed by its id)

Revision ID: 0029
Revises: 0028
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE receipt_lines ADD COLUMN hold boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE receipt_draft_lines ADD COLUMN hold boolean NOT NULL DEFAULT false")
    op.execute("""
        CREATE TABLE stock_releases (
            release_id uuid PRIMARY KEY,
            receipt_line_id uuid NOT NULL REFERENCES receipt_lines (receipt_line_id),
            batch_id uuid NOT NULL REFERENCES stock_batches (batch_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            units integer NOT NULL CHECK (units > 0),
            send_status varchar(10) NOT NULL DEFAULT 'off'
                CHECK (send_status IN ('off', 'pending', 'sent', 'refused')),
            send_reason varchar(300),
            send_attempts integer NOT NULL DEFAULT 0,
            created_by uuid REFERENCES users (user_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX stock_releases_line ON stock_releases (receipt_line_id)")
    op.execute("CREATE INDEX stock_releases_pending ON stock_releases (send_status) "
               "WHERE send_status = 'pending'")


def downgrade() -> None:
    op.execute("DROP TABLE stock_releases")
    op.execute("ALTER TABLE receipt_draft_lines DROP COLUMN hold")
    op.execute("ALTER TABLE receipt_lines DROP COLUMN hold")
