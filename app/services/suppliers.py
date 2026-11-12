"""Suppliers and the phones that may sign in to the supplier app (staff side)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone import InvalidPhone, normalise_mobile
from app.models import Supplier, SupplierUser
from app.services import audit, supplier_auth
from app.services.errors import Conflict, NotFound, SupplyError


async def get(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFound("supplier not found")
    return supplier


async def create(db: AsyncSession, *, name: str, notes: str | None,
                 user_id: uuid.UUID) -> Supplier:
    name = name.strip()
    taken = await db.scalar(select(Supplier).where(func.lower(Supplier.name) == name.lower()))
    if taken:
        raise Conflict("a supplier with this name already exists")
    supplier = Supplier(name=name, notes=notes, created_by=user_id)
    db.add(supplier)
    await db.flush()
    await audit.record(db, user_id=user_id, action="supplier.create", entity="supplier",
                       entity_id=supplier.supplier_id, after={"name": name})
    await db.refresh(supplier, ["users"])
    return supplier


async def update(db: AsyncSession, supplier_id: uuid.UUID, *, changes: dict,
                 user_id: uuid.UUID) -> Supplier:
    if any(v is None for k, v in changes.items() if k != "notes"):
        raise SupplyError("name and active cannot be empty")
    supplier = await get(db, supplier_id)
    before = {k: getattr(supplier, k) for k in changes}
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        taken = await db.scalar(select(Supplier).where(
            func.lower(Supplier.name) == changes["name"].lower(),
            Supplier.supplier_id != supplier_id))
        if taken:
            raise Conflict("a supplier with this name already exists")
    for key, value in changes.items():
        setattr(supplier, key, value)
    if changes.get("is_active") is False:
        for u in supplier.users:
            await supplier_auth.revoke_all_for_user(db, u.supplier_user_id)
    await audit.record(db, user_id=user_id, action="supplier.update", entity="supplier",
                       entity_id=supplier_id, before=before, after=changes)
    await db.flush()
    return supplier


async def add_user(db: AsyncSession, supplier_id: uuid.UUID, *, phone: str, full_name: str,
                   language: str, user_id: uuid.UUID) -> SupplierUser:
    await get(db, supplier_id)
    try:
        phone = normalise_mobile(phone)
    except InvalidPhone as exc:
        raise SupplyError(str(exc)) from None
    if await db.scalar(select(SupplierUser).where(SupplierUser.phone == phone)):
        raise Conflict("this phone number is already registered to a supplier")
    su = SupplierUser(supplier_id=supplier_id, phone=phone, full_name=full_name.strip(),
                      language=language)
    db.add(su)
    await db.flush()
    await audit.record(db, user_id=user_id, action="supplier_user.create",
                       entity="supplier_user", entity_id=su.supplier_user_id,
                       after={"supplier_id": str(supplier_id), "phone": phone})
    return su


async def update_user(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                      *, changes: dict, user_id: uuid.UUID) -> SupplierUser:
    su = await db.get(SupplierUser, supplier_user_id)
    if su is None or su.supplier_id != supplier_id:
        raise NotFound("supplier user not found")
    if any(v is None for v in changes.values()):
        raise SupplyError("fields cannot be empty")
    before = {k: getattr(su, k) for k in changes}
    for key, value in changes.items():
        setattr(su, key, value)
    if changes.get("is_active") is False:
        await supplier_auth.revoke_all_for_user(db, supplier_user_id)
    await audit.record(db, user_id=user_id, action="supplier_user.update",
                       entity="supplier_user", entity_id=supplier_user_id,
                       before=before, after=changes)
    await db.flush()
    return su
