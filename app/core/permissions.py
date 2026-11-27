"""Who may do what (PLAN.md decisions 9 and 16). Roles are fixed in code so the rules are
reviewable in one place. Supplier-app users are NOT staff and have no role here: their
access is the supplier id inside their own token type (see api/deps.py)."""

READ_ALL = {
    "branches.read",
    "suppliers.read",
    "matches.read",
    "orders.read",
    "base_prices.read",
    "partners.read",  # fulfilment partner: sourcing list, share, price alerts (0030)
}

# Stock (moved from Finance, joint-go-live phase B): receiving, transfers, write-offs,
# counts. Losses are approved by the accountant up to the write-off limit, the owner above.
# labels.print: register a run of QR label numbers and download its PDF (phase C).
STOCK_WORK = {"stock.read", "products.read", "grn.create", "stock.transfer", "stock.adjust",
              "labels.print"}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        READ_ALL
        | {
            "branches.write",
            "suppliers.write",
            "matches.write",
            "online.sync",
            "orders.create",
            "orders.approve",
            "users.read",
            "users.write",
            "audit.read",
            "products.write",
            "stock.approve",
            "settings.write",
            "suppliers.bank",
            "partners.write",
        }
        | STOCK_WORK
    ),
    # Compares offers, confirms matches, places orders, receives stock.
    "purchasing": frozenset(READ_ALL | STOCK_WORK | {"suppliers.write", "matches.write",
                                                     "online.sync", "orders.create",
                                                     "products.write", "partners.write"}),
    # Raises requests (draft orders) for their own branch; sees suppliers.
    "branch_manager": frozenset(READ_ALL | STOCK_WORK | {"orders.request"}),
    # Read-only: price history, orders.
    "accountant": frozenset(READ_ALL | {"audit.read", "stock.read", "products.read",
                                        "stock.approve", "suppliers.bank"}),
}

ROLES = tuple(ROLE_PERMISSIONS)

# Roles that see every branch regardless of their branch list.
ALL_BRANCH_ROLES = {"owner", "purchasing", "accountant"}

# Roles whose order submit needs nobody else's approval (Finance rule: the owner's own
# document counts as approved, audited).
SELF_APPROVING_ROLES = {"owner"}

# Every staff role uses two-factor sign-in (when TWO_FACTOR is on).
TOTP_REQUIRED_ROLES = set(ROLES)


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
