"""Fulfilment partner (owner 2026-11-27, docs/plans/fulfilment-partner-2026-11-27.md): a shop
that fills, order by order, the products a branch lists but does not carry (first: Nimali
Stores for Matugama). Phase P1: who the partner is, where it serves, which products it fills,
and its price to us + stock per product.

- suppliers.kind: 'wholesale' (every supplier so far) | 'fulfilment_partner'
- supplier_branches: the branches a partner serves (a partner serves nothing else)
- online_branch_listings: which Online products a branch lists (from the catalogue sync)
- branch_sourcing: per (branch, Online product) 'own' or 'partner'; no row = own = as today
- partner_items: per pack size, the partner's price to us and its stock count
- partner_price_changes: every price change the partner makes (staff alert)

Nothing existing changes meaning: with these tables empty the system behaves as before (G1/G3).

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
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            created_by uuid REFERENCES users (user_id),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (supplier_id, branch_id)
        )""")
    op.execute("""
        CREATE TABLE online_branch_listings (
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_product_id uuid NOT NULL REFERENCES online_products (online_product_id),
            synced_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, online_product_id)
        )""")
    op.execute("""
        CREATE TABLE branch_sourcing (
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_product_id uuid NOT NULL REFERENCES online_products (online_product_id),
            mode varchar(8) NOT NULL CHECK (mode IN ('own', 'partner')),
            partner_id uuid REFERENCES suppliers (supplier_id),
            changed_by uuid REFERENCES users (user_id),
            changed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (branch_id, online_product_id),
            CHECK ((mode = 'partner') = (partner_id IS NOT NULL))
        )""")
    op.execute("CREATE INDEX branch_sourcing_partner ON branch_sourcing (partner_id, branch_id) "
               "WHERE mode = 'partner'")
    op.execute("""
        CREATE TABLE partner_items (
            item_id uuid PRIMARY KEY,
            supplier_id uuid NOT NULL REFERENCES suppliers (supplier_id),
            branch_id uuid NOT NULL REFERENCES branches (branch_id),
            online_product_id uuid NOT NULL REFERENCES online_products (online_product_id),
            online_variant_id uuid NOT NULL REFERENCES online_variants (online_variant_id),
            price_to_us numeric(12, 2) CHECK (price_to_us > 0),
            stock_qty integer NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
            updated_by uuid REFERENCES supplier_users (supplier_user_id),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (supplier_id, branch_id, online_variant_id)
        )""")
    op.execute("""
        CREATE TABLE partner_price_changes (
            change_id bigserial PRIMARY KEY,
            item_id uuid NOT NULL REFERENCES partner_items (item_id),
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


def downgrade() -> None:
    op.execute("DROP TABLE partner_price_changes")
    op.execute("DROP TABLE partner_items")
    op.execute("DROP TABLE branch_sourcing")
    op.execute("DROP TABLE online_branch_listings")
    op.execute("DROP TABLE supplier_branches")
    op.execute("ALTER TABLE suppliers DROP COLUMN kind")
