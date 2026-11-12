"""Online product mirror + supplier offer -> Online product matches (PLAN.md decision 6).

online_products is a read-only copy of SRIBEESonline's catalogue (synced; never edited
here). A product that disappears from Online is marked inactive, never deleted, because
matches and (later) orders point at it.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE online_products (
            online_product_id uuid PRIMARY KEY,
            sku               varchar(100),
            name              varchar(255) NOT NULL,
            name_si           varchar(255),
            display_size      varchar(80),
            display_size_unit varchar(32),
            size_amount       numeric(14,3),
            size_kind         varchar(8) CHECK (size_kind IN ('mass', 'volume', 'count')),
            is_active         boolean NOT NULL DEFAULT true,
            synced_at         timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_online_products_sku ON online_products (sku);

        CREATE TABLE online_variants (
            online_variant_id uuid PRIMARY KEY,
            online_product_id uuid NOT NULL REFERENCES online_products(online_product_id),
            sku               varchar(100),
            name              varchar(120) NOT NULL DEFAULT '',
            weight_grams      integer,
            is_active         boolean NOT NULL DEFAULT true
        );
        CREATE INDEX ix_online_variants_product ON online_variants (online_product_id);

        CREATE TABLE offer_matches (
            match_id          bigserial PRIMARY KEY,
            offer_id          uuid NOT NULL,
            supplier_id       uuid NOT NULL,
            online_product_id uuid NOT NULL REFERENCES online_products(online_product_id),
            online_variant_id uuid REFERENCES online_variants(online_variant_id),
            status            varchar(10) NOT NULL
                CHECK (status IN ('suggested', 'confirmed', 'rejected')),
            score             numeric(4,3),
            decided_by        uuid REFERENCES users(user_id),
            decided_at        timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (offer_id, supplier_id)
                REFERENCES supplier_offers (offer_id, supplier_id),
            CHECK ((status = 'suggested') = (decided_by IS NULL))
        );
        -- One row per (offer, product, variant); one confirmed match per offer.
        CREATE UNIQUE INDEX uq_offer_matches_pair ON offer_matches
            (offer_id, online_product_id,
             coalesce(online_variant_id, '00000000-0000-0000-0000-000000000000'::uuid));
        CREATE UNIQUE INDEX uq_offer_matches_one_confirmed ON offer_matches (offer_id)
            WHERE status = 'confirmed';
        CREATE INDEX ix_offer_matches_supplier ON offer_matches (supplier_id, status);

        CREATE TABLE sync_state (
            key        varchar(40) PRIMARY KEY,
            value      jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    raise NotImplementedError
