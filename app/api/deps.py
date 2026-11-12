"""Request dependencies: the signed-in user, permission checks, branch scope."""

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.db import get_db
from app.core.permissions import ALL_BRANCH_ROLES, has_permission
from app.models import Supplier, SupplierSession, SupplierUser, User, UserSession

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    session_id: uuid.UUID

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.user_id

    @property
    def role(self) -> str:
        return self.user.role

    def branch_scope(self) -> set[uuid.UUID] | None:
        """None = every branch; otherwise the branches this user may see."""
        if self.user.role in ALL_BRANCH_ROLES or self.user.all_branches:
            return None
        return {b.branch_id for b in self.user.branches}

    def can_see_branch(self, branch_id: uuid.UUID) -> bool:
        scope = self.branch_scope()
        return scope is None or branch_id in scope


def client_ip(request: Request) -> str | None:
    # The app only listens on the internal network behind Caddy, which APPENDS the real
    # peer to X-Forwarded-For. Earlier entries come from the client and can be forged
    # (they would let anyone dodge the per-IP code limit), so take the LAST one.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:64]
    return request.client.host if request.client else None


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    unauthorised = HTTPException(
        status_code=401, detail="sign in required", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None:
        raise unauthorised
    try:
        claims = security.read_token(creds.credentials, security.ACCESS)
        user_id = uuid.UUID(claims["sub"])
        session_id = uuid.UUID(claims["sid"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise unauthorised from None

    session = await db.get(UserSession, session_id)
    if session is None or session.revoked_at is not None or session.user_id != user_id:
        raise unauthorised
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorised
    return Principal(user=user, session_id=session_id)


def require(permission: str):
    async def checker(principal: Principal = Depends(current_principal)) -> Principal:
        if not has_permission(principal.role, permission):
            raise HTTPException(status_code=403, detail="you do not have access to this")
        return principal

    return checker


def scoped_branches(principal: Principal, branch_id: uuid.UUID | None) -> set[uuid.UUID] | None:
    """Branch filter for a report: the requested branch (if allowed) within the user's scope."""
    scope = principal.branch_scope()
    if branch_id is not None:
        if not principal.can_see_branch(branch_id):
            raise HTTPException(status_code=403, detail="you do not have access to this branch")
        return {branch_id}
    return scope


# ---------------------------------------------------------------- supplier app
@dataclass
class SupplierPrincipal:
    """A signed-in supplier-app user. Every supplier endpoint filters by supplier_id from
    here, never from the request body or URL (PLAN.md: supplier isolation)."""

    user: SupplierUser
    session_id: uuid.UUID

    @property
    def supplier_id(self) -> uuid.UUID:
        return self.user.supplier_id


async def current_supplier(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> SupplierPrincipal:
    unauthorised = HTTPException(
        status_code=401, detail="sign in required", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None:
        raise unauthorised
    try:
        claims = security.read_token(creds.credentials, security.SUPPLIER)
        user_id = uuid.UUID(claims["sub"])
        supplier_id = uuid.UUID(claims["sup"])
        session_id = uuid.UUID(claims["sid"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise unauthorised from None

    session = await db.get(SupplierSession, session_id)
    if session is None or session.revoked_at is not None or session.supplier_user_id != user_id:
        raise unauthorised
    user = await db.get(SupplierUser, user_id)
    if user is None or not user.is_active or user.supplier_id != supplier_id:
        raise unauthorised
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise unauthorised
    return SupplierPrincipal(user=user, session_id=session_id)
