"""Supplier master data: the details Finance needs (code, contact, terms, bank).

Entered once here and sent to Finance, so nobody types them twice. The code is numbered by
the system (SUP-0001, never reused). Existing suppliers are numbered in creation order.

When this branch is merged with the stock phase (0008, 0009), set down_revision to "0009".

Revision ID: 0010
"""

from alembic import op

revision = "0010"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE supplier_code_seq;
        ALTER TABLE suppliers
            ADD COLUMN code               varchar(20),
            ADD COLUMN contact_name       varchar(150),
            ADD COLUMN phone              varchar(20),
            ADD COLUMN email              varchar(255),
            ADD COLUMN address            text,
            ADD COLUMN credit_days        smallint NOT NULL DEFAULT 0
                CHECK (credit_days BETWEEN 0 AND 365),
            ADD COLUMN is_related_party   boolean NOT NULL DEFAULT false,
            ADD COLUMN bank_name          varchar(100),
            ADD COLUMN bank_branch        varchar(100),
            ADD COLUMN bank_account_name  varchar(150),
            ADD COLUMN bank_account_no    varchar(40);
        UPDATE suppliers s SET code = 'SUP-' || lpad(n.rn::text, 4, '0')
          FROM (SELECT supplier_id, row_number() OVER (ORDER BY created_at, supplier_id) AS rn
                  FROM suppliers) n
         WHERE s.supplier_id = n.supplier_id;
        SELECT setval('supplier_code_seq', GREATEST((SELECT count(*) FROM suppliers), 1),
                      (SELECT count(*) FROM suppliers) > 0);
        ALTER TABLE suppliers ALTER COLUMN code SET NOT NULL;
        CREATE UNIQUE INDEX uq_suppliers_code ON suppliers (code);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX uq_suppliers_code;
        ALTER TABLE suppliers
            DROP COLUMN code, DROP COLUMN contact_name, DROP COLUMN phone,
            DROP COLUMN email, DROP COLUMN address, DROP COLUMN credit_days,
            DROP COLUMN is_related_party, DROP COLUMN bank_name, DROP COLUMN bank_branch,
            DROP COLUMN bank_account_name, DROP COLUMN bank_account_no;
        DROP SEQUENCE supplier_code_seq;
        """
    )
