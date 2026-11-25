"""Supplier app sign-in with email + password (owner, 2026-11-25).

Staff set a temporary password when they create the supplier (or add a person); the
supplier changes it at the first sign-in. The SMS code sign-in stays. Wrong-password
lockout counters live on the person's row; the password itself is an argon2 hash only.

Revision ID: 0011
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE supplier_users
            ADD COLUMN email                varchar(255),
            ADD COLUMN password_hash        text,
            ADD COLUMN must_change_password boolean NOT NULL DEFAULT false,
            ADD COLUMN password_changed_at  timestamptz,
            ADD COLUMN failed_logins        smallint NOT NULL DEFAULT 0,
            ADD COLUMN locked_until         timestamptz;
        ALTER TABLE supplier_sessions ADD COLUMN via_password boolean NOT NULL DEFAULT false;
        CREATE UNIQUE INDEX uq_supplier_users_email ON supplier_users (lower(email))
            WHERE email IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE supplier_sessions DROP COLUMN via_password;
        DROP INDEX uq_supplier_users_email;
        ALTER TABLE supplier_users
            DROP COLUMN email, DROP COLUMN password_hash, DROP COLUMN must_change_password,
            DROP COLUMN password_changed_at, DROP COLUMN failed_logins,
            DROP COLUMN locked_until;
        """
    )
