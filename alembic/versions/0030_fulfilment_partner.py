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
            cancel_reason varchar(200)
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
            UNIQUE (request_id, order_item_id)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE fulfilment_lines")
    op.execute("DROP TABLE fulfilment_requests")
    op.execute("DROP TABLE partner_price_changes")
    op.execute("DROP TABLE partner_items")
    op.execute("DROP TABLE online_branch_listings")
    op.execute("DROP TABLE supplier_branches")
    op.execute("ALTER TABLE suppliers DROP COLUMN kind")
