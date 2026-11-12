"""Foundation: branches, staff users + sessions, suppliers, supplier-app users, one-time
codes, supplier sessions, append-only audit log.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE branches (
            branch_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code             varchar(20)  NOT NULL UNIQUE,
            name             varchar(120) NOT NULL,
            -- SRIBEESonline's branch id, once synced; nothing is hard-wired to a branch.
            online_branch_id integer UNIQUE,
            is_active        boolean NOT NULL DEFAULT true,
            created_at       timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE users (
            user_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email           varchar(255) NOT NULL,
            full_name       varchar(150) NOT NULL,
            password_hash   varchar(255) NOT NULL,
            role            varchar(20)  NOT NULL
                CHECK (role IN ('owner', 'purchasing', 'branch_manager', 'accountant')),
            all_branches    boolean NOT NULL DEFAULT false,
            is_active       boolean NOT NULL DEFAULT true,
            totp_secret_enc text,
            totp_enabled    boolean NOT NULL DEFAULT false,
            failed_logins   integer NOT NULL DEFAULT 0,
            locked_until    timestamptz,
            last_login_at   timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX uq_users_email ON users (lower(email));

        CREATE TABLE user_branches (
            user_id   uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            branch_id uuid NOT NULL REFERENCES branches(branch_id),
            PRIMARY KEY (user_id, branch_id)
        );

        CREATE TABLE user_sessions (
            session_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      uuid NOT NULL REFERENCES users(user_id),
            refresh_hash varchar(64) NOT NULL UNIQUE,
            user_agent   varchar(300),
            ip_address   varchar(64),
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz
        );
        CREATE INDEX ix_user_sessions_user ON user_sessions (user_id);

        CREATE TABLE suppliers (
            supplier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        varchar(150) NOT NULL,
            notes       text,
            is_active   boolean NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now(),
            created_by  uuid REFERENCES users(user_id)
        );
        CREATE UNIQUE INDEX uq_suppliers_name ON suppliers (lower(name));

        -- People who sign in to the supplier app. A phone belongs to one supplier only.
        CREATE TABLE supplier_users (
            supplier_user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_id      uuid NOT NULL REFERENCES suppliers(supplier_id),
            phone            varchar(12) NOT NULL UNIQUE CHECK (phone ~ '^\\+947[0-9]{8}$'),
            full_name        varchar(150) NOT NULL,
            language         varchar(2) NOT NULL DEFAULT 'si' CHECK (language IN ('si', 'en')),
            is_active        boolean NOT NULL DEFAULT true,
            last_login_at    timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_supplier_users_supplier ON supplier_users (supplier_id);

        -- One-time codes. Rows are kept for rate limits and the audit trail; the code
        -- itself is only an HMAC.
        CREATE TABLE supplier_otps (
            otp_id           bigserial PRIMARY KEY,
            phone            varchar(12) NOT NULL,
            supplier_user_id uuid REFERENCES supplier_users(supplier_user_id),
            code_hash        varchar(64),
            ip_address       varchar(64),
            created_at       timestamptz NOT NULL DEFAULT now(),
            expires_at       timestamptz NOT NULL,
            attempts         integer NOT NULL DEFAULT 0,
            used_at          timestamptz
        );
        CREATE INDEX ix_supplier_otps_phone ON supplier_otps (phone, created_at DESC);
        CREATE INDEX ix_supplier_otps_ip ON supplier_otps (ip_address, created_at DESC);

        CREATE TABLE supplier_sessions (
            session_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            supplier_user_id uuid NOT NULL REFERENCES supplier_users(supplier_user_id),
            refresh_hash     varchar(64) NOT NULL UNIQUE,
            user_agent       varchar(300),
            ip_address       varchar(64),
            created_at       timestamptz NOT NULL DEFAULT now(),
            last_used_at     timestamptz NOT NULL DEFAULT now(),
            expires_at       timestamptz NOT NULL,
            revoked_at       timestamptz
        );
        CREATE INDEX ix_supplier_sessions_user ON supplier_sessions (supplier_user_id);

        CREATE TABLE audit_log (
            audit_id         bigserial PRIMARY KEY,
            occurred_at      timestamptz NOT NULL DEFAULT now(),
            user_id          uuid REFERENCES users(user_id),
            supplier_user_id uuid REFERENCES supplier_users(supplier_user_id),
            action           varchar(60) NOT NULL,
            entity           varchar(40) NOT NULL,
            entity_id        varchar(64),
            before           jsonb,
            after            jsonb,
            ip_address       varchar(64)
        );
        CREATE INDEX ix_audit_entity ON audit_log (entity, entity_id);

        -- The audit trail can only grow.
        CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit log rows cannot be changed or deleted'
                USING ERRCODE = 'restrict_violation';
        END $$;
        CREATE TRIGGER trg_audit_log_append_only
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
        """
    )


def downgrade() -> None:
    raise NotImplementedError("foundation cannot be downgraded")
