"""Suppliers and the phones that may sign in to the supplier app (staff side)."""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone import InvalidPhone, normalise_mobile
from app.models import OutboxEvent, Supplier, SupplierUser
from app.services import audit, supplier_auth
from app.services.errors import Conflict, NotFound, SupplyError


async def get(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFound("supplier not found")
    return supplier


BANK_FIELDS = ("bank_name", "bank_branch", "bank_account_name", "bank_account_no")
DETAIL_FIELDS = ("contact_name", "phone", "email", "address", "credit_days",
                 "is_related_party")


def _clean(changes: dict) -> dict:
    """Trim text; an empty string means 'not set'."""
    out = {}
    for k, v in changes.items():
        if isinstance(v, str):
            v = v.strip() or None
        out[k] = v
    return out


def _mask_account(no: str | None) -> str | None:
    """Audit rows keep only the last four characters of an account number."""
    return None if not no else "*" * max(len(no) - 4, 0) + no[-4:]


def _audit_view(values: dict) -> dict:
    out = dict(values)
    if "bank_account_no" in out:
        out["bank_account_no"] = _mask_account(out["bank_account_no"])
    return out


def finance_view(supplier: Supplier) -> dict:
    """The supplier object Finance receives (internal, token-protected feed)."""
    phones = [u.phone for u in supplier.users if u.is_active]
    return {
        "supplier_id": str(supplier.supplier_id), "code": supplier.code, "name": supplier.name,
        "is_active": supplier.is_active, "notes": supplier.notes,
        "contact_name": supplier.contact_name, "email": supplier.email,
        "phone": supplier.phone or (phones[0] if phones else None),
        "address": supplier.address, "credit_days": supplier.credit_days,
        "is_related_party": supplier.is_related_party,
        "bank_name": supplier.bank_name, "bank_branch": supplier.bank_branch,
        "bank_account_name": supplier.bank_account_name,
        "bank_account_no": supplier.bank_account_no,
    }


def _emit_updated(db: AsyncSession, supplier: Supplier) -> None:
    db.add(OutboxEvent(event="supplier.updated", payload={
        "supplier_id": str(supplier.supplier_id), "supplier": finance_view(supplier)}))


async def create(db: AsyncSession, *, name: str, notes: str | None, user_id: uuid.UUID,
                 details: dict | None = None, app_password: str | None = None,
                 app_language: str = "si") -> Supplier:
    name = name.strip()
    taken = await db.scalar(select(Supplier).where(func.lower(Supplier.name) == name.lower()))
    if taken:
        raise Conflict("a supplier with this name already exists")
    details = _clean(details or {})
    if app_password and not (details.get("phone") and details.get("email")):
        raise SupplyError("a login needs the supplier's phone and email")
    number = await db.scalar(text("SELECT nextval('supplier_code_seq')"))
    supplier = Supplier(name=name, notes=notes, created_by=user_id,
                        code=f"SUP-{number:04d}", **details)
    db.add(supplier)
    await db.flush()
    await audit.record(db, user_id=user_id, action="supplier.create", entity="supplier",
                       entity_id=supplier.supplier_id,
                       after=_audit_view({"name": name, "code": supplier.code, **details}))
    if app_password:
        await add_user(db, supplier.supplier_id, phone=details["phone"],
                       full_name=details.get("contact_name") or name, language=app_language,
                       user_id=user_id, email=details["email"], password=app_password)
    await db.refresh(supplier, ["users"])
    _emit_updated(db, supplier)
    return supplier


async def update(db: AsyncSession, supplier_id: uuid.UUID, *, changes: dict,
                 user_id: uuid.UUID) -> Supplier:
    changes = _clean(changes)
    if any(v is None for k, v in changes.items()
           if k in ("name", "is_active", "credit_days", "is_related_party")):
        raise SupplyError("name, active, credit days and related party cannot be empty")
    supplier = await get(db, supplier_id)
    before = {k: getattr(supplier, k) for k in changes}
    if "name" in changes:
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
    _emit_updated(db, supplier)
    return supplier


async def update_bank(db: AsyncSession, supplier_id: uuid.UUID, *, changes: dict,
                      user_id: uuid.UUID) -> Supplier:
    """Bank details: owner and accountant only (checked in the API). The audit row keeps
    the last four characters of the account number, never the whole number."""
    changes = _clean(changes)
    supplier = await get(db, supplier_id)
    before = {k: getattr(supplier, k) for k in changes}
    for key, value in changes.items():
        setattr(supplier, key, value)
    await audit.record(db, user_id=user_id, action="supplier.bank_update", entity="supplier",
                       entity_id=supplier_id, before=_audit_view(before),
                       after=_audit_view(changes))
    await db.flush()
    _emit_updated(db, supplier)
    return supplier


async def add_user(db: AsyncSession, supplier_id: uuid.UUID, *, phone: str, full_name: str,
                   language: str, user_id: uuid.UUID, email: str | None = None,
                   password: str | None = None) -> SupplierUser:
    await get(db, supplier_id)
    try:
        phone = normalise_mobile(phone)
    except InvalidPhone as exc:
        raise SupplyError(str(exc)) from None
    if bool(email) != bool(password):
        raise SupplyError("give both an email and a password, or neither")
    if await db.scalar(select(SupplierUser).where(SupplierUser.phone == phone)):
        raise Conflict("this phone number is already registered to a supplier")
    if email:
        email = supplier_auth.normalise_email(email)
        if await db.scalar(select(SupplierUser).where(
                func.lower(SupplierUser.email) == email)):
            raise Conflict("this email is already used for a supplier login")
    su = SupplierUser(supplier_id=supplier_id, phone=phone, full_name=full_name.strip(),
                      language=language, email=email)
    db.add(su)
    await db.flush()
    if password:
        await supplier_auth.set_password(db, su, password, temporary=True)
    await audit.record(db, user_id=user_id, action="supplier_user.create",
                       entity="supplier_user", entity_id=su.supplier_user_id,
                       after={"supplier_id": str(supplier_id), "phone": phone,
                              "email": email, "has_password": bool(password)})
    return su


async def set_user_password(db: AsyncSession, supplier_id: uuid.UUID,
                            supplier_user_id: uuid.UUID, *, password: str,
                            user_id: uuid.UUID) -> SupplierUser:
    """Staff set a new temporary password (the supplier forgot it, or no login existed
    yet). The person must have an email; they change the password at the next sign-in."""
    su = await db.get(SupplierUser, supplier_user_id)
    if su is None or su.supplier_id != supplier_id:
        raise NotFound("supplier user not found")
    if not su.email:
        raise SupplyError("this person has no login email; set one first")
    await supplier_auth.set_password(db, su, password, temporary=True)
    await audit.record(db, user_id=user_id, action="supplier_user.password_set",
                       entity="supplier_user", entity_id=supplier_user_id,
                       after={"temporary": True})
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
