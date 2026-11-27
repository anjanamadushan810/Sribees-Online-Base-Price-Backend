"""Automatic draft orders (owner 2026-11-26): when a branch's stock falls below its
minimum, a draft order up to the maximum is prepared per supplier, in units.

- supply_orders.is_auto_draft marks the draft the reorder run keeps adding to;
- main_suppliers: a supplier staff chose for an item (else the one we bought it from last);
- reorder_runs: what each run did and what it left out, for the dashboard.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE supply_orders ADD COLUMN is_auto_draft boolean NOT NULL DEFAULT false")
    op.execute("""
        CREATE TABLE main_suppliers (
            online_variant_id uuid PRIMARY KEY REFERENCES online_variants(online_variant_id),
            supplier_id       uuid NOT NULL REFERENCES suppliers(supplier_id),
            set_by            uuid NOT NULL REFERENCES users(user_id),
            set_at            timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE TABLE reorder_runs (
            run_id     bigserial PRIMARY KEY,
            branch_id  uuid NOT NULL REFERENCES branches(branch_id),
            ran_by     uuid REFERENCES users(user_id),
            ran_at     timestamptz NOT NULL DEFAULT now(),
            result     jsonb NOT NULL
        )""")
    op.execute("CREATE INDEX ix_reorder_runs_branch ON reorder_runs (branch_id, ran_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE reorder_runs")
    op.execute("DROP TABLE main_suppliers")
    op.execute("ALTER TABLE supply_orders DROP COLUMN is_auto_draft")
