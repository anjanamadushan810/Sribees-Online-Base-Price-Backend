"""Finance's month status, kept in Supply (joint-go-live plan, phase B): stock does not
move, and no journal is asked for, in a month Finance has closed. Filled from Finance's
period.closed / period.reopened events.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE finance_periods (
            period_id   uuid PRIMARY KEY,
            start_date  date NOT NULL,
            end_date    date NOT NULL,
            status      varchar(10) NOT NULL CHECK (status IN ('open', 'closed')),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CHECK (end_date >= start_date)
        );
        CREATE INDEX ix_finance_periods_dates ON finance_periods (start_date, end_date);
        -- the journal shim's "asked already?" lookup (review 2026-11-13 #7)
        CREATE INDEX ix_supply_outbox_journal_ref ON supply_outbox
            ((payload->>'source'), (payload->>'source_ref'))
            WHERE event = 'journal.requested';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_supply_outbox_journal_ref; DROP TABLE finance_periods")
