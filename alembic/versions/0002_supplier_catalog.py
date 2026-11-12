"""Supplier catalogue: sections, products, offers, append-only price history, and the
supplier's WhatsApp message header/footer (PLAN.md §3, decisions 13-14).

Isolation in the database itself: every child row carries supplier_id and a composite
foreign key to its parent's (id, supplier_id), so a row can never point at another
supplier's section or product, whatever the application does.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE suppliers
            ADD COLUMN price_list_header text NOT NULL DEFAULT '',
            ADD COLUMN price_list_footer text NOT NULL DEFAULT '';

        CREATE TABLE supplier_sections (
            section_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_id uuid NOT NULL REFERENCES suppliers(supplier_id),
            title       varchar(150) NOT NULL CHECK (btrim(title) <> ''),
            notes       jsonb NOT NULL DEFAULT '[]'::jsonb,
            sort_order  integer NOT NULL DEFAULT 0,
            is_active   boolean NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (section_id, supplier_id)
        );
        CREATE UNIQUE INDEX uq_supplier_sections_title
            ON supplier_sections (supplier_id, lower(title));

        CREATE TABLE supplier_products (
            product_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_id  uuid NOT NULL,
            section_id   uuid NOT NULL,
            heading      varchar(200) NOT NULL CHECK (btrim(heading) <> ''),
            notes_before jsonb NOT NULL DEFAULT '[]'::jsonb,
            notes_after  jsonb NOT NULL DEFAULT '[]'::jsonb,
            sort_order   integer NOT NULL DEFAULT 0,
            is_active    boolean NOT NULL DEFAULT true,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (product_id, supplier_id),
            FOREIGN KEY (section_id, supplier_id)
                REFERENCES supplier_sections (section_id, supplier_id)
        );
        CREATE INDEX ix_supplier_products_section ON supplier_products (section_id, sort_order);

        -- One price line. units_per_box may be unknown (the supplier did not say);
        -- such an offer cannot be ordered until it is filled in.
        CREATE TABLE supplier_offers (
            offer_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_id   uuid NOT NULL,
            product_id    uuid NOT NULL,
            label         varchar(120) NOT NULL DEFAULT '',
            price         numeric(12,2) NOT NULL CHECK (price > 0),
            mrp           numeric(12,2) CHECK (mrp IS NULL OR mrp > 0),
            units_per_box integer CHECK (units_per_box IS NULL OR units_per_box > 0),
            extra         jsonb NOT NULL DEFAULT '[]'::jsonb,
            sort_order    integer NOT NULL DEFAULT 0,
            is_active     boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (offer_id, supplier_id),
            FOREIGN KEY (product_id, supplier_id)
                REFERENCES supplier_products (product_id, supplier_id)
        );
        CREATE INDEX ix_supplier_offers_product ON supplier_offers (product_id, sort_order);
        CREATE INDEX ix_supplier_offers_supplier ON supplier_offers (supplier_id);

        -- Every price the supplier ever gave, in order. Never changed or deleted.
        CREATE TABLE supplier_offer_prices (
            history_id       bigserial PRIMARY KEY,
            offer_id         uuid NOT NULL,
            supplier_id      uuid NOT NULL,
            price            numeric(12,2) NOT NULL,
            mrp              numeric(12,2),
            units_per_box    integer,
            source           varchar(10) NOT NULL CHECK (source IN ('import', 'app', 'staff')),
            supplier_user_id uuid REFERENCES supplier_users(supplier_user_id),
            user_id          uuid REFERENCES users(user_id),
            changed_at       timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (offer_id, supplier_id)
                REFERENCES supplier_offers (offer_id, supplier_id),
            CHECK (supplier_user_id IS NOT NULL OR user_id IS NOT NULL)
        );
        CREATE INDEX ix_offer_prices_offer ON supplier_offer_prices (offer_id, changed_at DESC);

        CREATE FUNCTION offer_prices_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'price history rows cannot be changed or deleted'
                USING ERRCODE = 'restrict_violation';
        END $$;
        CREATE TRIGGER trg_offer_prices_append_only
            BEFORE UPDATE OR DELETE ON supplier_offer_prices
            FOR EACH ROW EXECUTE FUNCTION offer_prices_append_only();
        """
    )


def downgrade() -> None:
    raise NotImplementedError
