"""Who may do what (PLAN.md decisions 9 and 16). Roles are fixed in code so the rules are
reviewable in one place. Supplier-app users are NOT staff and have no role here: their
access is the supplier id inside their own token type (see api/deps.py)."""

READ_ALL = {
    "branches.read",
    "suppliers.read",
    "matches.read",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        READ_ALL
        | {
            "branches.write",
            "suppliers.write",
            "matches.write",
            "online.sync",
            "users.read",
            "users.write",
            "audit.read",
        }
    ),
    # Compares offers, confirms matches, places orders, receives stock.
    "purchasing": frozenset(READ_ALL | {"suppliers.write", "matches.write", "online.sync"}),
    # Raises requests for their own branch; sees suppliers.
    "branch_manager": frozenset(READ_ALL),
    # Read-only: price history, orders.
    "accountant": frozenset(READ_ALL | {"audit.read"}),
}

ROLES = tuple(ROLE_PERMISSIONS)

# Roles that see every branch regardless of their branch list.
ALL_BRANCH_ROLES = {"owner", "purchasing", "accountant"}

# Every staff role uses two-factor sign-in (when TWO_FACTOR is on).
TOTP_REQUIRED_ROLES = set(ROLES)


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
