"""Supply <-> Finance PO link (docs/plans/joint-go-live.md, phase A).

- supply_outbox.event_id: a stable id per event, so Finance books each one exactly once.
- supply_orders: 'rejected' (Finance's owner said no; the supplier never saw it),
  finance_po_no, reject_reason, approved_by_name (the Finance user who approved);
  status widened for the back-order states of phase B.
- branches.online_branch_id becomes a uuid: Online's branch ids are uuids, and Finance
  keys its branches by them (it was an integer by mistake; no row had a usable value).
- feed_state / processed_events: Supply's cursor over Finance's outbox, same rules as
  Finance's own feed (one transaction per event, never twice).

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE supply_outbox
            ADD COLUMN event_id uuid NOT NULL DEFAULT gen_random_uuid();
        ALTER TABLE supply_outbox ADD CONSTRAINT uq_supply_outbox_event UNIQUE (event_id);

        ALTER TABLE supply_orders DROP CONSTRAINT supply_orders_status_check;
        ALTER TABLE supply_orders ALTER COLUMN status TYPE varchar(16);
        ALTER TABLE supply_orders ADD CONSTRAINT supply_orders_status_check CHECK (status IN
            ('draft', 'submitted', 'approved', 'rejected', 'accepted', 'declined',
             'cancelled', 'received'));
        ALTER TABLE supply_orders
            ADD COLUMN finance_po_no    varchar(20),
            ADD COLUMN reject_reason    text,
            ADD COLUMN approved_by_name varchar(150),
            ADD COLUMN rejected_at      timestamptz;
        ALTER TABLE supply_orders ADD CONSTRAINT ck_supply_orders_rejected
            CHECK (status <> 'rejected' OR (reject_reason IS NOT NULL AND rejected_at IS NOT NULL));

        ALTER TABLE branches ALTER COLUMN online_branch_id TYPE uuid USING NULL;
        CREATE UNIQUE INDEX uq_branches_online ON branches (online_branch_id)
            WHERE online_branch_id IS NOT NULL;

        CREATE TABLE feed_state (
            feed           varchar(20) PRIMARY KEY,
            last_seq       bigint NOT NULL DEFAULT 0,
            gaps           jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_polled_at timestamptz,
            last_event_at  timestamptz,
            last_error     text,
            updated_at     timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO feed_state (feed) VALUES ('finance');

        CREATE TABLE processed_events (
            event_id     uuid PRIMARY KEY,
            feed         varchar(20) NOT NULL,
            seq          bigint NOT NULL,
            event_type   varchar(40) NOT NULL,
            outcome      varchar(12) NOT NULL,
            processed_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    raise NotImplementedError
