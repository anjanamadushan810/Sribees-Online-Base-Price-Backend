"""Product correction (owner 2026-11-26): stock received on the wrong product moves to the
right one. Two moves at the same value (1300 -> 1300, no journal), like a repack.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

OLD = ("'grn','supplier_return','transfer_out','transfer_in','writeoff','count','dispatch',"
       "'undispatch','return','opening','revalue','repack_out','repack_in','use'")
NEW = OLD + ",'correct_out','correct_in'"


def upgrade() -> None:
    op.execute("ALTER TABLE stock_moves DROP CONSTRAINT stock_moves_move_type_check")
    op.execute(f"ALTER TABLE stock_moves ADD CONSTRAINT stock_moves_move_type_check "
               f"CHECK (move_type IN ({NEW}))")


def downgrade() -> None:
    op.execute("ALTER TABLE stock_moves DROP CONSTRAINT stock_moves_move_type_check")
    op.execute(f"ALTER TABLE stock_moves ADD CONSTRAINT stock_moves_move_type_check "
               f"CHECK (move_type IN ({OLD}))")
