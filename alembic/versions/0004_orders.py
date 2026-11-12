"""Supplier orders, base prices and the outbox (PLAN.md decisions 5, 7, 10, 11, 15-17).

Order life:  draft -> submitted -> approved -> accepted | declined -> (received, phase 5b)
             cancelled from draft/submitted/approved/accepted (with a reason).
The supplier sees an order only from 'approved' on.

Base price = what we actually agreed to pay: one row per order line when the supplier
accepts; voided if the order is cancelled. Current base price = newest non-void row.

supply_outbox: events for Finance (PO) and for supplier notifications, consumed later
by sequence number (same idea as Online's finance_outbox).

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE supply_order_no_seq;

        CREATE TABLE supply_orders (
            order_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            order_no         varchar(20) NOT NULL UNIQUE
                DEFAULT 'SPO-' || lpad(nextval('supply_order_no_seq')::text, 6, '0'),
            supplier_id      uuid NOT NULL REFERENCES suppliers(supplier_id),
            branch_id        uuid NOT NULL REFERENCES branches(branch_id),
            status           varchar(10) NOT NULL DEFAULT 'draft' CHECK (status IN
                ('draft', 'submitted', 'approved', 'accepted', 'declined', 'cancelled',
                 'received')),
            note             text,
            delivery_charge  numeric(12,2) CHECK (delivery_charge IS NULL OR delivery_charge >= 0),
            supplier_note    text,
            cancel_reason    text,
            created_by       uuid NOT NULL REFERENCES users(user_id),
            submitted_by     uuid REFERENCES users(user_id),
            approved_by      uuid REFERENCES users(user_id),
            created_at       timestamptz NOT NULL DEFAULT now(),
            submitted_at     timestamptz,
            approved_at      timestamptz,
            supplier_seen_at timestamptz,
            answered_at      timestamptz,
            answered_by      uuid REFERENCES supplier_users(supplier_user_id),
            cancelled_at     timestamptz,
            cancelled_by     uuid REFERENCES users(user_id),
            UNIQUE (order_id, supplier_id),
            CHECK (status <> 'cancelled' OR (cancel_reason IS NOT NULL AND cancelled_at IS NOT NULL))
        );
        CREATE INDEX ix_supply_orders_supplier ON supply_orders (supplier_id, status);
        CREATE INDEX ix_supply_orders_branch ON supply_orders (branch_id, status);

        -- Prices are frozen on the line: a later price change never alters an order.
        CREATE TABLE supply_order_lines (
            line_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id          uuid NOT NULL,
            supplier_id       uuid NOT NULL,
            offer_id          uuid NOT NULL,
            online_product_id uuid NOT NULL REFERENCES online_products(online_product_id),
            online_variant_id uuid REFERENCES online_variants(online_variant_id),
            heading           varchar(200) NOT NULL,
            label             varchar(120) NOT NULL DEFAULT '',
            boxes             integer NOT NULL CHECK (boxes > 0),
            units_per_box     integer NOT NULL CHECK (units_per_box > 0),
            units             integer GENERATED ALWAYS AS (boxes * units_per_box) STORED,
            unit_price        numeric(12,2) NOT NULL CHECK (unit_price > 0),
            mrp               numeric(12,2),
            line_total        numeric(14,2)
                GENERATED ALWAYS AS (boxes * units_per_box * unit_price) STORED,
            FOREIGN KEY (order_id, supplier_id) REFERENCES supply_orders (order_id, supplier_id)
                ON DELETE CASCADE,
            FOREIGN KEY (offer_id, supplier_id) REFERENCES supplier_offers (offer_id, supplier_id),
            UNIQUE (order_id, offer_id)
        );

        -- Lines can change only while the order is a draft.
        CREATE FUNCTION order_lines_only_in_draft() RETURNS trigger LANGUAGE plpgsql AS $$
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
        CREATE TRIGGER trg_order_lines_only_in_draft
            BEFORE INSERT OR UPDATE OR DELETE ON supply_order_lines
            FOR EACH ROW EXECUTE FUNCTION order_lines_only_in_draft();

        CREATE TABLE base_prices (
            base_price_id     bigserial PRIMARY KEY,
            online_product_id uuid NOT NULL REFERENCES online_products(online_product_id),
            online_variant_id uuid REFERENCES online_variants(online_variant_id),
            supplier_id       uuid NOT NULL REFERENCES suppliers(supplier_id),
            order_id          uuid NOT NULL REFERENCES supply_orders(order_id),
            line_id           uuid NOT NULL UNIQUE REFERENCES supply_order_lines(line_id),
            unit_price        numeric(12,2) NOT NULL,
            mrp               numeric(12,2),
            recorded_at       timestamptz NOT NULL DEFAULT now(),
            voided_at         timestamptz
        );
        CREATE INDEX ix_base_prices_product ON base_prices
            (online_product_id, online_variant_id, recorded_at DESC) WHERE voided_at IS NULL;

        CREATE TABLE supply_outbox (
            seq        bigserial PRIMARY KEY,
            event      varchar(40) NOT NULL,
            payload    jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE FUNCTION supply_outbox_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'outbox rows cannot be changed or deleted'
                USING ERRCODE = 'restrict_violation';
        END $$;
        CREATE TRIGGER trg_supply_outbox_append_only
            BEFORE UPDATE OR DELETE ON supply_outbox
            FOR EACH ROW EXECUTE FUNCTION supply_outbox_append_only();
        """
    )


def downgrade() -> None:
    raise NotImplementedError
