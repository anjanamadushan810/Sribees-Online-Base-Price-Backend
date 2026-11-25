"""Staff-side management: branches, staff users, suppliers and their app phones."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.api.schemas import (
    BANK_FIELDS,
    BranchIn,
    BranchOut,
    SupplierBankPatch,
    SupplierFullOut,
    SupplierIn,
    SupplierPatch,
    SupplierUserIn,
    SupplierUserOut,
    SupplierUserPatch,
    UserIn,
    UserOut,
)
from app.core.db import get_db
from app.core.permissions import has_permission
from app.core.security import hash_password
from app.models import Branch, Supplier, User
from app.services import audit, suppliers
from app.services.errors import Conflict, SupplyError

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------- branches
@router.get("/branches", response_model=list[BranchOut])
async def list_branches(_: Principal = Depends(require("branches.read")),
                        db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Branch).order_by(Branch.name))).all()


@router.post("/branches", response_model=BranchOut, status_code=201)
async def create_branch(body: BranchIn, p: Principal = Depends(require("branches.write")),
                        db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(Branch).where(Branch.code == body.code)):
        raise Conflict("a branch with this code already exists")
    branch = Branch(**body.model_dump())
    db.add(branch)
    await db.flush()
    await audit.record(db, user_id=p.user_id, action="branch.create", entity="branch",
                       entity_id=branch.branch_id, after=body.model_dump(mode="json"))
    return branch


# ---------------------------------------------------------------- staff users
@router.get("/users", response_model=list[UserOut])
async def list_users(_: Principal = Depends(require("users.read")),
                     db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(User).order_by(User.full_name))).all()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserIn, p: Principal = Depends(require("users.write")),
                      db: AsyncSession = Depends(get_db)):
    email = body.email.strip().lower()
    if await db.scalar(select(User).where(func.lower(User.email) == email)):
        raise Conflict("a user with this email already exists")
    branches = []
    for bid in body.branch_ids:
        branch = await db.get(Branch, bid)
        if branch is None:
            raise SupplyError("unknown branch")
        branches.append(branch)
    user = User(email=email, full_name=body.full_name, role=body.role,
                password_hash=hash_password(body.password), all_branches=body.all_branches,
                branches=branches)
    db.add(user)
    await db.flush()
    await audit.record(db, user_id=p.user_id, action="user.create", entity="user",
                       entity_id=user.user_id, after={"email": email, "role": body.role})
    return user


# ---------------------------------------------------------------- suppliers
def _supplier_json(supplier: Supplier, p: Principal) -> dict:
    """Bank fields are left out of the response (not nulled) for roles without
    suppliers.bank."""
    exclude = set() if has_permission(p.role, "suppliers.bank") else set(BANK_FIELDS)
    return SupplierFullOut.model_validate(supplier).model_dump(mode="json", exclude=exclude)


@router.get("/suppliers")
async def list_suppliers(p: Principal = Depends(require("suppliers.read")),
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Supplier).order_by(Supplier.name))).all()
    return [_supplier_json(r, p) for r in rows]


@router.post("/suppliers", status_code=201)
async def create_supplier(body: SupplierIn, p: Principal = Depends(require("suppliers.write")),
                          db: AsyncSession = Depends(get_db)):
    details = body.model_dump(exclude={"name", "notes"})
    if any(details.get(f) for f in BANK_FIELDS) and not has_permission(p.role, "suppliers.bank"):
        raise HTTPException(status_code=403, detail="you do not have access to bank details")
    supplier = await suppliers.create(db, name=body.name, notes=body.notes,
                                      user_id=p.user_id, details=details)
    return _supplier_json(supplier, p)


@router.get("/suppliers/{supplier_id}")
async def get_supplier(supplier_id: uuid.UUID,
                       p: Principal = Depends(require("suppliers.read")),
                       db: AsyncSession = Depends(get_db)):
    return _supplier_json(await suppliers.get(db, supplier_id), p)


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(supplier_id: uuid.UUID, body: SupplierPatch,
                          p: Principal = Depends(require("suppliers.write")),
                          db: AsyncSession = Depends(get_db)):
    supplier = await suppliers.update(db, supplier_id,
                                      changes=body.model_dump(exclude_unset=True),
                                      user_id=p.user_id)
    return _supplier_json(supplier, p)


@router.patch("/suppliers/{supplier_id}/bank")
async def update_supplier_bank(supplier_id: uuid.UUID, body: SupplierBankPatch,
                               p: Principal = Depends(require("suppliers.bank")),
                               db: AsyncSession = Depends(get_db)):
    supplier = await suppliers.update_bank(db, supplier_id,
                                           changes=body.model_dump(exclude_unset=True),
                                           user_id=p.user_id)
    return _supplier_json(supplier, p)


@router.post("/suppliers/{supplier_id}/users", response_model=SupplierUserOut, status_code=201)
async def add_supplier_user(supplier_id: uuid.UUID, body: SupplierUserIn,
                            p: Principal = Depends(require("suppliers.write")),
                            db: AsyncSession = Depends(get_db)):
    return await suppliers.add_user(db, supplier_id, phone=body.phone,
                                    full_name=body.full_name, language=body.language,
                                    user_id=p.user_id)


@router.patch("/suppliers/{supplier_id}/users/{supplier_user_id}",
              response_model=SupplierUserOut)
async def update_supplier_user(supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                               body: SupplierUserPatch,
                               p: Principal = Depends(require("suppliers.write")),
                               db: AsyncSession = Depends(get_db)):
    return await suppliers.update_user(db, supplier_id, supplier_user_id,
                                       changes=body.model_dump(exclude_unset=True),
                                       user_id=p.user_id)
