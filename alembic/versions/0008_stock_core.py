"""Stock moves to Supply, part 1 (joint-go-live plan, phase B): settings, document numbers,
stock products, batches, moves, transfers, write-offs and counts.

Moved from Finance (0002, 0004, 0014, 0016) with the same columns, checks and triggers,
so the moved code and tests run unchanged. Differences, all because Supply has no ledger:
journal_id columns are plain UUIDs (the id of the `journal.requested` event that asks
Finance to post the journal), not foreign keys to a journals table.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


SCHEMA = r"""
-- Thresholds the owner can change without a release.
CREATE TABLE settings (
    key          VARCHAR(60) PRIMARY KEY,
    value        NUMERIC(14,2) NOT NULL CHECK (value >= 0),
    description  VARCHAR(300) NOT NULL,
    updated_by   UUID REFERENCES users(user_id),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO settings (key, value, description) VALUES
    ('writeoff_approval_limit', 5000, 'Stock write-offs above this need approval'),
    ('repack_normal_waste_pct', 5,
     'Repack waste up to this % of the bulk weight is normal and stays in the packs'' cost');

-- Document numbers (TRF-000001, ADJ-000001, ...).
CREATE TABLE doc_counters (
    prefix   VARCHAR(10) PRIMARY KEY,
    next_no  INTEGER NOT NULL DEFAULT 1
);

-- A head office keeps no stock (Finance rule, moved with the code).
ALTER TABLE branches ADD COLUMN is_head_office BOOLEAN NOT NULL DEFAULT false;

-- ---------------------------------------------------------------- stock products
-- One row per SRIBEESonline variant: product_id = Online's variant id (the id Finance
-- and the Online feed use). Bulk and material items have ids of their own.
CREATE TABLE products (
    product_id        UUID PRIMARY KEY,
    sku               VARCHAR(100),
    name              VARCHAR(255) NOT NULL,
    unit              VARCHAR(10)  NOT NULL DEFAULT 'each' CHECK (unit IN ('each','kg')),
    track_expiry      BOOLEAN      NOT NULL DEFAULT false,
    is_active         BOOLEAN      NOT NULL DEFAULT true,
    category          VARCHAR(100),
    stock_mode        VARCHAR(8)   NOT NULL DEFAULT 'unit'
                                   CHECK (stock_mode IN ('unit','loose','packed')),
    parent_product_id UUID REFERENCES products(product_id),
    weight_grams      INTEGER CHECK (weight_grams > 0),
    online_cost       NUMERIC(14,2) CHECK (online_cost >= 0),
    kind              VARCHAR(10) NOT NULL DEFAULT 'sellable'
                                   CHECK (kind IN ('sellable','bulk','material')),
    weight_kg         NUMERIC(10,3) CHECK (weight_kg > 0),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT products_mode_unit CHECK (
        (stock_mode = 'loose') = (unit = 'kg')
        AND (parent_product_id IS NULL OR (stock_mode = 'packed' AND weight_grams IS NOT NULL))),
    CONSTRAINT products_kind_unit
        CHECK ((kind <> 'bulk' OR unit = 'kg') AND (kind <> 'material' OR unit = 'each'))
);
CREATE INDEX ix_products_name ON products (lower(name));

-- ---------------------------------------------------------------- batches and moves
CREATE TABLE stock_batches (
    batch_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_no       VARCHAR(30) NOT NULL UNIQUE,
    branch_id      UUID NOT NULL REFERENCES branches(branch_id),
    product_id     UUID NOT NULL REFERENCES products(product_id),
    supplier_id    UUID REFERENCES suppliers(supplier_id),
    received_date  DATE NOT NULL,
    expiry_date    DATE,
    unit_cost      NUMERIC(14,4) NOT NULL CHECK (unit_cost >= 0),
    qty_received   NUMERIC(14,3) NOT NULL CHECK (qty_received > 0),
    qty_on_hand    NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (qty_on_hand >= 0),
    value_on_hand  NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (value_on_hand >= 0),
    status         VARCHAR(12) NOT NULL DEFAULT 'available'
                     CHECK (status IN ('available','quarantine')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- an empty batch carries no value
    CHECK (qty_on_hand > 0 OR value_on_hand = 0)
);
CREATE INDEX ix_stock_batches_pick ON stock_batches (branch_id, product_id, expiry_date)
    WHERE qty_on_hand > 0;

CREATE TABLE stock_moves (
    move_id     BIGSERIAL PRIMARY KEY,
    batch_id    UUID NOT NULL REFERENCES stock_batches(batch_id),
    move_date   DATE NOT NULL,
    move_type   VARCHAR(20) NOT NULL,
    qty         NUMERIC(14,3) NOT NULL,
    value       NUMERIC(14,2) NOT NULL,
    doc_type    VARCHAR(20) NOT NULL,
    doc_id      UUID NOT NULL,
    journal_id  UUID,
    created_by  UUID REFERENCES users(user_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stock_moves_move_type_check CHECK (move_type IN
        ('grn','supplier_return','transfer_out','transfer_in','writeoff','count','dispatch',
         'undispatch','return','opening','revalue','repack_out','repack_in','use')),
    -- only a revaluation moves value without quantity
    CONSTRAINT stock_moves_qty_check CHECK (qty <> 0 OR (move_type = 'revalue' AND value <> 0)),
    -- quantity and value move the same way (or value is zero, e.g. free issue)
    CONSTRAINT stock_moves_check
        CHECK ((qty > 0 AND value >= 0) OR (qty < 0 AND value <= 0) OR move_type = 'revalue')
);
CREATE INDEX ix_stock_moves_batch ON stock_moves (batch_id);
CREATE INDEX ix_stock_moves_doc ON stock_moves (doc_type, doc_id);

-- ---------------------------------------------------------------- transfers, losses, counts
CREATE TABLE stock_transfers (
    transfer_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_no       VARCHAR(20) NOT NULL UNIQUE,
    from_branch_id    UUID NOT NULL REFERENCES branches(branch_id),
    to_branch_id      UUID NOT NULL REFERENCES branches(branch_id),
    status            VARCHAR(20) NOT NULL CHECK (status IN
                        ('in_transit','received','short_pending','closed')),
    shipped_date      DATE NOT NULL,
    received_date     DATE,
    value_shipped     NUMERIC(14,2) NOT NULL CHECK (value_shipped >= 0),
    value_received    NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (value_received >= 0),
    notes             TEXT,
    shipped_by        UUID REFERENCES users(user_id),
    received_by       UUID REFERENCES users(user_id),
    ship_journal_id   UUID,
    receive_journal_id UUID,
    adjustment_id     UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_branch_id <> to_branch_id),
    CHECK (value_received <= value_shipped)
);

CREATE TABLE transfer_lines (
    transfer_line_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id       UUID NOT NULL REFERENCES stock_transfers(transfer_id),
    line_no           SMALLINT NOT NULL,
    from_batch_id     UUID NOT NULL REFERENCES stock_batches(batch_id),
    qty               NUMERIC(14,3) NOT NULL CHECK (qty > 0),
    value             NUMERIC(14,2) NOT NULL CHECK (value >= 0),
    qty_received      NUMERIC(14,3) CHECK (qty_received >= 0),
    to_batch_id       UUID REFERENCES stock_batches(batch_id),
    UNIQUE (transfer_id, line_no),
    CHECK (qty_received IS NULL OR qty_received <= qty)
);

CREATE TABLE stock_adjustments (
    adjustment_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    adjustment_no   VARCHAR(20) NOT NULL UNIQUE,
    branch_id       UUID NOT NULL REFERENCES branches(branch_id),
    kind            VARCHAR(20) NOT NULL CHECK (kind IN ('writeoff','count','transfer_short')),
    reason          VARCHAR(20) NOT NULL CHECK (reason IN
                      ('expired','damaged','theft','count','transit','other')),
    adjustment_date DATE NOT NULL,
    status          VARCHAR(20) NOT NULL CHECK (status IN
                      ('pending_approval','posted','rejected')),
    -- estimated at request, exact once posted; negative = a loss
    value           NUMERIC(14,2) NOT NULL,
    notes           TEXT,
    requested_by    UUID REFERENCES users(user_id),
    approved_by     UUID REFERENCES users(user_id),
    approved_at     TIMESTAMPTZ,
    rejected_reason VARCHAR(500),
    journal_id      UUID,
    source_id       UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_stock_adjustments_pending ON stock_adjustments (branch_id)
    WHERE status = 'pending_approval';
ALTER TABLE stock_transfers ADD CONSTRAINT fk_transfer_adjustment
    FOREIGN KEY (adjustment_id) REFERENCES stock_adjustments(adjustment_id);

CREATE TABLE adjustment_lines (
    adjustment_line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    adjustment_id      UUID NOT NULL REFERENCES stock_adjustments(adjustment_id),
    line_no            SMALLINT NOT NULL,
    batch_id           UUID NOT NULL REFERENCES stock_batches(batch_id),
    qty_change         NUMERIC(14,3) NOT NULL CHECK (qty_change <> 0),
    value              NUMERIC(14,2),
    UNIQUE (adjustment_id, line_no),
    UNIQUE (adjustment_id, batch_id)
);

CREATE TABLE stock_counts (
    count_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    count_no        VARCHAR(20) NOT NULL UNIQUE,
    branch_id       UUID NOT NULL REFERENCES branches(branch_id),
    count_date      DATE NOT NULL,
    status          VARCHAR(20) NOT NULL CHECK (status IN ('open','submitted','cancelled')),
    notes           TEXT,
    started_by      UUID REFERENCES users(user_id),
    submitted_by    UUID REFERENCES users(user_id),
    adjustment_id   UUID REFERENCES stock_adjustments(adjustment_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One open count per branch at a time.
CREATE UNIQUE INDEX uq_stock_counts_open ON stock_counts (branch_id) WHERE status = 'open';

CREATE TABLE count_lines (
    count_id     UUID NOT NULL REFERENCES stock_counts(count_id),
    batch_id     UUID NOT NULL REFERENCES stock_batches(batch_id),
    system_qty   NUMERIC(14,3) NOT NULL CHECK (system_qty >= 0),
    counted_qty  NUMERIC(14,3) CHECK (counted_qty >= 0),
    PRIMARY KEY (count_id, batch_id)
);

-- ---------------------------------------------------------------- receiving (replaces GRN)
-- Goods arrive against an order, in as many receipts as the supplier needs (decision 31:
-- a short delivery is a back-order). The supplier's invoice is entered here (decision 23).
CREATE TABLE receipts (
    receipt_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_no     VARCHAR(20) NOT NULL UNIQUE,
    order_id       UUID NOT NULL REFERENCES supply_orders(order_id),
    supplier_id    UUID NOT NULL REFERENCES suppliers(supplier_id),
    branch_id      UUID NOT NULL REFERENCES branches(branch_id),
    received_date  DATE NOT NULL,
    invoice_no     VARCHAR(60),
    invoice_total  NUMERIC(14,2) CHECK (invoice_total >= 0),
    notes          TEXT,
    total          NUMERIC(14,2) NOT NULL CHECK (total >= 0),
    journal_id     UUID,
    received_by    UUID REFERENCES users(user_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_receipts_order ON receipts (order_id);

CREATE TABLE receipt_lines (
    receipt_line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id      UUID NOT NULL REFERENCES receipts(receipt_id),
    line_no         SMALLINT NOT NULL,
    order_line_id   UUID NOT NULL REFERENCES supply_order_lines(line_id),
    product_id      UUID NOT NULL REFERENCES products(product_id),
    -- in the order's units (pieces)
    units           INTEGER NOT NULL CHECK (units > 0),
    free_units      INTEGER NOT NULL DEFAULT 0 CHECK (free_units >= 0),
    -- in the product's unit: pieces, or kilograms for a weighed item (paid + free)
    qty             NUMERIC(14,3) NOT NULL CHECK (qty > 0),
    unit_price      NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
    line_value      NUMERIC(14,2) NOT NULL CHECK (line_value >= 0),
    expiry_date     DATE,
    mfg_date        DATE,
    mrp             NUMERIC(12,2) CHECK (mrp > 0),
    batch_id        UUID NOT NULL UNIQUE REFERENCES stock_batches(batch_id),
    UNIQUE (receipt_id, line_no),
    CHECK (mfg_date IS NULL OR expiry_date IS NULL OR mfg_date < expiry_date)
);
CREATE INDEX ix_receipt_lines_order_line ON receipt_lines (order_line_id);

-- "Close the rest" of a back-order by hand, with a reason (decision 31).
ALTER TABLE supply_orders
    ADD COLUMN rest_closed_reason TEXT,
    ADD COLUMN rest_closed_at     TIMESTAMPTZ,
    ADD COLUMN rest_closed_by     UUID REFERENCES users(user_id);
"""


TRIGGERS = r"""
-- A batch starts empty; only stock moves fill or drain it.
CREATE FUNCTION sup_guard_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.qty_on_hand <> 0 OR NEW.value_on_hand <> 0 THEN
            RAISE EXCEPTION 'a batch starts empty; stock arrives through a stock move'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'stock batches are never deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.qty_on_hand <> OLD.qty_on_hand OR NEW.value_on_hand <> OLD.value_on_hand)
       AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'batch % quantity changes only through a stock move', OLD.batch_no
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.branch_id <> OLD.branch_id OR NEW.product_id <> OLD.product_id
       OR NEW.unit_cost <> OLD.unit_cost OR NEW.qty_received <> OLD.qty_received THEN
        RAISE EXCEPTION 'batch % identity and cost are fixed', OLD.batch_no
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_stock_batches_guard
    BEFORE INSERT OR UPDATE OR DELETE ON stock_batches
    FOR EACH ROW EXECUTE FUNCTION sup_guard_batch();


CREATE FUNCTION sup_apply_stock_move() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE stock_batches
       SET qty_on_hand = qty_on_hand + NEW.qty,
           value_on_hand = value_on_hand + NEW.value,
           updated_at = now()
     WHERE batch_id = NEW.batch_id;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_stock_moves_apply
    AFTER INSERT ON stock_moves
    FOR EACH ROW EXECUTE FUNCTION sup_apply_stock_move();


CREATE FUNCTION sup_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER trg_stock_moves_append_only
    BEFORE UPDATE OR DELETE ON stock_moves
    FOR EACH ROW EXECUTE FUNCTION sup_append_only();
CREATE TRIGGER trg_stock_moves_no_truncate
    BEFORE TRUNCATE ON stock_moves
    FOR EACH STATEMENT EXECUTE FUNCTION sup_append_only();

-- What was received is history: a mistake is corrected by a new document, never an edit.
CREATE TRIGGER trg_receipt_lines_append_only
    BEFORE UPDATE OR DELETE ON receipt_lines
    FOR EACH ROW EXECUTE FUNCTION sup_append_only();
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    op.execute(TRIGGERS)


def downgrade() -> None:
    raise RuntimeError("the stock migration is not reversible; restore from backup")
