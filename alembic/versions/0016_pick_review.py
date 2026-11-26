"""Pack scan review fixes (2026-11-26):
- picks.qty_returned: how much of a consumed pick came back, so a weighed bag is never
  returned twice (review #5);
- online_event_failures: an Online event Supply could not apply is kept here for staff and
  the reading position moves on, so one bad event never blocks the rest (review #2).

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE picks ADD COLUMN qty_returned numeric(14,3) NOT NULL DEFAULT 0
            CHECK (qty_returned >= 0 AND qty_returned <= qty);
        CREATE TABLE online_event_failures (
            seq          bigint PRIMARY KEY,
            event_id     uuid,
            event_type   varchar(40) NOT NULL,
            order_id     uuid,
            error        text NOT NULL,
            payload      jsonb NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            resolved_at  timestamptz,
            resolved_by  uuid REFERENCES users(user_id)
        );
        CREATE INDEX ix_online_event_failures_open ON online_event_failures (created_at)
            WHERE resolved_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE online_event_failures; ALTER TABLE picks DROP COLUMN qty_returned")
