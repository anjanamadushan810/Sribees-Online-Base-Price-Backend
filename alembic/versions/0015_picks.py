"""Pack scan (joint-go-live plan, phase E; decisions 24, 25): an Online order's stock is
taken by scanning the unit's QR label at packing, never by FEFO.

A pick ties one scanned label to one Online order line. It holds the unit for that order
(the batch shows it as reserved) until Online says what happened:
    delivered  -> consumed: the stock leaves the batch at its cost (Dr 5000 / Cr 1300)
    cancelled  -> released: the unit is free again
    returned   -> returned: the unit goes back into the same batch (Dr 1300 / Cr 5000)
Goods kept by weight have one label per supplier bag: a pick then names the bag and the
kilograms taken, and the same bag serves many orders.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE picks (
            pick_id          uuid PRIMARY KEY,
            online_order_id  uuid NOT NULL,
            order_number     varchar(40),
            order_item_id    uuid NOT NULL,
            online_branch_id uuid NOT NULL,
            product_id       uuid NOT NULL REFERENCES products(product_id),
            label            varchar(10) NOT NULL REFERENCES labels(number),
            batch_id         uuid NOT NULL REFERENCES stock_batches(batch_id),
            qty              numeric(14,3) NOT NULL CHECK (qty > 0),
            by_weight        boolean NOT NULL DEFAULT false,
            status           varchar(10) NOT NULL DEFAULT 'picked'
                             CHECK (status IN ('picked', 'released', 'consumed', 'returned')),
            picked_by        varchar(120),
            picked_at        timestamptz NOT NULL DEFAULT now(),
            settled_at       timestamptz,
            move_id          bigint REFERENCES stock_moves(move_id),
            return_move_id   bigint REFERENCES stock_moves(move_id),
            journal_id       uuid
        );
        CREATE INDEX ix_picks_order ON picks (online_order_id, order_item_id);
        CREATE INDEX ix_picks_batch_open ON picks (batch_id) WHERE status = 'picked';
        -- a piece's label is on one order at a time, and is sold once
        CREATE UNIQUE INDEX ux_picks_label_once ON picks (label)
            WHERE NOT by_weight AND status IN ('picked', 'consumed');

        -- Online's events read so far (seq of /internal/supply/events)
        INSERT INTO sync_state (key, value) VALUES ('online_events', '{"seq": 0}')
            ON CONFLICT (key) DO NOTHING;

        -- A returned unit keeps its sticker: a used label may go back to linked (only
        -- on the same batch; the batch guard below still holds).
        CREATE OR REPLACE FUNCTION sup_guard_label() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'labels are never deleted' USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.number <> OLD.number OR NEW.range_id <> OLD.range_id THEN
                RAISE EXCEPTION 'label % is fixed', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.batch_id IS NOT NULL AND NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
                RAISE EXCEPTION 'label % is already on another batch', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.status = 'void' AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'label % is void', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.status IN ('linked', 'used') AND NEW.status = 'printed' THEN
                RAISE EXCEPTION 'label % cannot be unlinked', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TABLE picks;
        DELETE FROM sync_state WHERE key = 'online_events';
        CREATE OR REPLACE FUNCTION sup_guard_label() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'labels are never deleted' USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.number <> OLD.number OR NEW.range_id <> OLD.range_id THEN
                RAISE EXCEPTION 'label % is fixed', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.batch_id IS NOT NULL AND NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
                RAISE EXCEPTION 'label % is already on another batch', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.status IN ('used', 'void') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'label % is %', OLD.number, OLD.status
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.status = 'linked' AND NEW.status = 'printed' THEN
                RAISE EXCEPTION 'label % cannot be unlinked', OLD.number
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
