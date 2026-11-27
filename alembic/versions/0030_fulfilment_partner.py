"""Fulfilment partner (owner 2026-11-27, docs/plans/fulfilment-partner-2026-11-27.md): a shop
that fills, order by order, the products a branch sells but does not carry (first: Nimali
Stores for Matugama).

Which products a branch carries is decided in SRIBEES Online (branch_inventory.sourcing_group:
'store' = Our Store, 'external_daily' / 'external_slow' = Other Products); Supply mirrors it.

- suppliers.kind: 'wholesale' (every supplier so far) | 'fulfilment_partner'
- supplier_branches: the branch a partner serves (one partner per branch)
- online_branch_listings: which Online products a branch sells, with Online's sourcing group
- partner_items: per pack size, the partner's price to us and its stock count
- partner_price_changes: every price change the partner makes, to us or to customers
  (staff alert)
- fulfilment_requests / fulfilment_lines: what one Online order needs from the partner

Nothing existing changes meaning: with no partner linked the system behaves as before (G1/G3).

Revision ID: 0030
Revises: 0029
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE suppliers ADD COLUMN kind varchar(20) NOT NULL DEFAULT 'wholesale' "
               "CHECK (kind IN ('wholesale', 'fulfilment_partner'))")
    # how Online keeps the product's stock (unit / loose / packed): a loose product's partner
    # count goes to Online in grams
    op.execute("ALTER TABLE online_products ADD COLUMN stock_mode varchar(8)")
    op.execute("""
        CREATE TABLE supplier_branches (
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL UNIQUE REFERENCES branches (branch_id),
            fills_store_shortfall boolean NOT NULL DEFAULT false,
            created_by uuid REFERENCES users (user_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (supplier_id, branch_id)
        )""")
    op.execute("""
        CREATE TABLE online_branch_listings (
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_product_id uuid NOT NULL REFERENCES online_products (online_product_id),
            sourcing_group varchar(20),
            synced_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, online_product_id)
        )""")
    op.execute("""
        CREATE TABLE partner_items (
            item_id uuid PRIMARY KEY,
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_product_id uuid NOT NULL REFERENCES online_products (online_product_id),
            online_variant_id uuid NOT NULL REFERENCES online_variants (online_variant_id),
            price_to_us numeric(12, 2) CHECK (price_to_us > 0),
            stock_qty integer NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
            online_stock_sent integer,
            updated_by uuid REFERENCES supplier_users (supplier_user_id),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (supplier_id, branch_id, online_variant_id)
        )""")
    op.execute("""
        CREATE TABLE partner_price_changes (
            change_id bigserial PRIMARY KEY,
            item_id uuid NOT NULL REFERENCES partner_items (item_id),
            kind varchar(10) NOT NULL DEFAULT 'to_us' CHECK (kind IN ('to_us', 'customer')),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            old_price numeric(12, 2),
            new_price numeric(12, 2) NOT NULL,
            changed_by uuid REFERENCES supplier_users (supplier_user_id),
            changed_at timestamptz NOT NULL DEFAULT now(),
            seen_by uuid REFERENCES users (user_id),
            seen_at timestamptz
        )""")
    op.execute("CREATE INDEX partner_price_changes_unseen ON partner_price_changes (branch_id) "
               "WHERE seen_at IS NULL")
    op.execute("""
        CREATE TABLE fulfilment_requests (
            request_id uuid PRIMARY KEY,
            request_no varchar(20) NOT NULL UNIQUE,
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_order_id uuid NOT NULL UNIQUE,
            order_number varchar(50),
            status varchar(10) NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'seen', 'ready', 'handed', 'cancelled')),
            customer_name varchar(200),
            customer_phone varchar(30),
            alternate_phone varchar(30),
            delivery_address text,
            order_notes text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            seen_at timestamptz,
            ready_at timestamptz,
            handed_at timestamptz,
            handed_by uuid REFERENCES users (user_id),
            cancelled_at timestamptz,
            cancel_reason varchar(200),
            -- P4: booked in the day's partner order (one Finance PO per day, F18)
            booked_at timestamptz,
            supply_order_id uuid REFERENCES supply_orders (order_id),
            -- the customer order was cancelled / sent back after the bag was handed over
            order_closed varchar(12) CHECK (order_closed IN ('cancelled', 'rto')),
            closed_at timestamptz,
            -- the order went out (packed / with the courier) but its bag was never scanned
            unscanned_at timestamptz,
            -- the owner took it out of the day order (booked by hand)
            excluded_reason varchar(300)
        )""")
    op.execute("CREATE INDEX fulfilment_requests_open ON fulfilment_requests "
               "(supplier_id, status) WHERE status IN ('new', 'seen', 'ready')")
    op.execute("CREATE INDEX fulfilment_requests_branch ON fulfilment_requests "
               "(branch_id, created_at)")
    op.execute("""
        CREATE TABLE fulfilment_lines (
            line_id uuid PRIMARY KEY,
            request_id uuid NOT NULL REFERENCES fulfilment_requests (request_id)
                ON DELETE CASCADE,
            order_item_id uuid NOT NULL,
            online_product_id uuid NOT NULL,
            online_variant_id uuid NOT NULL,
            name varchar(255) NOT NULL,
            variant_name varchar(120),
            qty integer NOT NULL CHECK (qty > 0),
            price_to_us numeric(12, 2),
            stock_taken integer NOT NULL DEFAULT 0 CHECK (stock_taken >= 0),
            ticked_qty integer NOT NULL DEFAULT 0 CHECK (ticked_qty >= 0),
            -- P4: the batch the day's receipt put it in, and units that came back to our shelf
            batch_id uuid REFERENCES stock_batches (batch_id),
            returned_qty integer NOT NULL DEFAULT 0 CHECK (returned_qty >= 0),
            expiry_date date,  -- typed at handover when staff see it on the pack
            -- units claimed back (cancel / refused / returned), before or after booking
            -- (returned_qty above); of the dispatched units, those the partner took back
            out_qty integer NOT NULL DEFAULT 0 CHECK (out_qty >= 0),
            -- stock measure per unit: 1 piece, or kilograms for goods kept by weight
            stock_per_unit numeric(10, 3) NOT NULL DEFAULT 1,
            out_value numeric(14, 2),
            back_qty integer NOT NULL DEFAULT 0 CHECK (back_qty >= 0),
            back_value numeric(14, 2) NOT NULL DEFAULT 0,
            CHECK (ticked_qty <= qty AND returned_qty <= qty),
            UNIQUE (request_id, order_item_id)
        )""")

    # owner 2026-11-28: goods sent back after the handover go back to the PARTNER: staff
    # hand them over, the partner accepts them in the app; booked goods become a supplier
    # credit in Finance (supplier_return.posted)
    op.execute("""
        CREATE TABLE partner_returns (
            return_id uuid PRIMARY KEY,
            return_no varchar(20) NOT NULL UNIQUE,
            request_id uuid NOT NULL REFERENCES fulfilment_requests (request_id),
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            reason varchar(12) NOT NULL
                CHECK (reason IN ('cancelled', 'rto', 'undelivered', 'returned')),
            status varchar(10) NOT NULL DEFAULT 'waiting'
                CHECK (status IN ('waiting', 'sent', 'accepted')),
            in_books boolean NOT NULL,
            event_ref varchar(120) NOT NULL UNIQUE,
            credit_total numeric(14, 2),
            created_at timestamptz NOT NULL DEFAULT now(),
            sent_at timestamptz,
            sent_by uuid REFERENCES users (user_id),
            accepted_at timestamptz,
            accepted_by uuid REFERENCES supplier_users (supplier_user_id)
        )""")
    op.execute("CREATE INDEX partner_returns_open ON partner_returns (branch_id, status) "
               "WHERE status <> 'accepted'")
    op.execute("""
        CREATE TABLE partner_return_lines (
            line_id uuid PRIMARY KEY,
            return_id uuid NOT NULL REFERENCES partner_returns (return_id) ON DELETE CASCADE,
            fr_line_id uuid NOT NULL REFERENCES fulfilment_lines (line_id),
            qty integer NOT NULL CHECK (qty > 0),
            value numeric(14, 2)
        )""")
    # the partner part of an Online event that failed: kept with its payload to retry
    op.execute("""
        CREATE TABLE partner_event_failures (
            failure_id bigserial PRIMARY KEY,
            seq bigint,
            event_type varchar(40),
            order_id uuid,
            payload jsonb NOT NULL,
            error text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            resolved_by uuid REFERENCES users (user_id)
        )""")
    # P5: the partner agreed with a week's statement (Mon-Sun); payment itself is Finance's
    op.execute("""
        CREATE TABLE partner_statement_confirms (
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            week_start date NOT NULL,
            total numeric(14, 2) NOT NULL,
            confirmed_at timestamptz NOT NULL DEFAULT now(),
            confirmed_by uuid REFERENCES supplier_users (supplier_user_id),
            PRIMARY KEY (supplier_id, branch_id, week_start)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE partner_statement_confirms")
    op.execute("DROP TABLE partner_event_failures")
    op.execute("DROP TABLE partner_return_lines")
    op.execute("DROP TABLE partner_returns")
    op.execute("DROP TABLE fulfilment_lines")
    op.execute("DROP TABLE fulfilment_requests")
    op.execute("DROP TABLE partner_price_changes")
    op.execute("DROP TABLE partner_items")
    op.execute("DROP TABLE online_branch_listings")
    op.execute("DROP TABLE supplier_branches")
    op.execute("ALTER TABLE online_products DROP COLUMN stock_mode")
    op.execute("ALTER TABLE suppliers DROP COLUMN kind")
