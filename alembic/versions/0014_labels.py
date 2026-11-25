"""QR labels (joint-go-live plan, phase C; decisions 18, 25, 27, 28).

Labels are printed in bulk on A4 sticker sheets and carry only a number (QR, no price).
Printing registers a range: every number in it becomes a `labels` row. At receiving (or the
go-live count) staff stick one on each unit and the number is linked to that batch. A label
links to exactly one batch, is never reused and never deleted. Packing (phase E) marks it used.

Number = 9 digits + 1 Luhn check digit, so a mistyped number is refused.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE SEQUENCE label_number_seq START WITH 100000000 MINVALUE 100000000
            MAXVALUE 999999999 NO CYCLE;

        CREATE TABLE label_ranges (
            range_id     uuid PRIMARY KEY,
            range_no     varchar(20) NOT NULL UNIQUE,
            first_body   bigint NOT NULL,
            last_body    bigint NOT NULL,
            count        integer NOT NULL CHECK (count > 0 AND count <= 10000),
            branch_id    uuid REFERENCES branches(branch_id),
            cols         integer NOT NULL,
            rows         integer NOT NULL,
            printed_by   uuid REFERENCES users(user_id),
            created_at   timestamptz NOT NULL DEFAULT now(),
            CHECK (last_body - first_body + 1 = count)
        );

        CREATE TABLE labels (
            number      varchar(10) PRIMARY KEY CHECK (number ~ '^[1-9][0-9]{9}$'),
            range_id    uuid NOT NULL REFERENCES label_ranges(range_id),
            status      varchar(8) NOT NULL DEFAULT 'printed'
                        CHECK (status IN ('printed', 'linked', 'used', 'void')),
            batch_id    uuid REFERENCES stock_batches(batch_id),
            linked_by   uuid REFERENCES users(user_id),
            linked_at   timestamptz,
            used_at     timestamptz,
            used_ref    varchar(80),
            CHECK ((status = 'printed') = (batch_id IS NULL) OR status = 'void')
        );
        CREATE INDEX ix_labels_batch ON labels (batch_id) WHERE batch_id IS NOT NULL;
        CREATE INDEX ix_labels_range ON labels (range_id, number);

        -- One batch only, never reused, never deleted.
        CREATE FUNCTION sup_guard_label() RETURNS trigger LANGUAGE plpgsql AS $$
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
        CREATE TRIGGER trg_labels_guard BEFORE UPDATE OR DELETE ON labels
            FOR EACH ROW EXECUTE FUNCTION sup_guard_label();

        CREATE FUNCTION sup_guard_label_range() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'label ranges are never changed or deleted'
                USING ERRCODE = 'restrict_violation';
        END $$;
        CREATE TRIGGER trg_label_ranges_guard BEFORE UPDATE OR DELETE ON label_ranges
            FOR EACH ROW EXECUTE FUNCTION sup_guard_label_range();

        INSERT INTO settings (key, value, description) VALUES
            ('label_sheet_cols', 8, 'QR labels across one A4 sticker sheet'),
            ('label_sheet_rows', 12, 'QR labels down one A4 sticker sheet'),
            ('labels_required', 0,
             '1 = every unit received needs a QR label (switch on at go-live)')
        ON CONFLICT (key) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM settings WHERE key IN ('label_sheet_cols', 'label_sheet_rows',
                                           'labels_required');
        DROP TABLE labels;
        DROP TABLE label_ranges;
        DROP FUNCTION sup_guard_label();
        DROP FUNCTION sup_guard_label_range();
        DROP SEQUENCE label_number_seq;
        """
    )
