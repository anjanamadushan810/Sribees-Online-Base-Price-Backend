"""Supplier app push (FCM): where to send a supplier's new-order alerts.

A device token belongs to one signed-in session; signing out (or the session being
revoked) drops it. feed_state 'push' is the push sender's cursor over supply_outbox.

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE supplier_devices (
            token            varchar(400) PRIMARY KEY,
            supplier_user_id uuid NOT NULL REFERENCES supplier_users(supplier_user_id),
            session_id       uuid NOT NULL REFERENCES supplier_sessions(session_id)
                             ON DELETE CASCADE,
            platform         varchar(20) NOT NULL DEFAULT 'android',
            created_at       timestamptz NOT NULL DEFAULT now(),
            last_seen_at     timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_supplier_devices_user ON supplier_devices (supplier_user_id);
        CREATE INDEX ix_supplier_devices_session ON supplier_devices (session_id);
        INSERT INTO feed_state (feed) VALUES ('push');
        """
    )


def downgrade() -> None:
    raise NotImplementedError
