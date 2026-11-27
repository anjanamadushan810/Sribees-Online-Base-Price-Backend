"""Product barcodes (owner plan 2026-11-27, P1): the EAN/UPC on a pack -> our Online pack size.

The first time staff scan a barcode we do not know, they pick our product once; after that the
barcode finds it. A wrong check digit is refused (a mis-read must never be linked). Moving a
barcode to another pack size is for purchasing / the owner (products.write), audited."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OnlineProduct, OnlineVariant, ProductBarcode
from app.services import audit
from app.services.actors import Actor
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

LENGTHS = (8, 12, 13, 14)  # EAN-8, UPC-A, EAN-13, GTIN-14


def gtin_check_ok(code: str) -> bool:
    """GS1 check digit: weights 3,1,3,... from the digit left of the check digit."""
    body, check = code[:-1], int(code[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalise(raw: str) -> str:
    """The checked barcode, or SupplyError saying why it is not one."""
    code = "".join(raw.split()).replace("-", "")
    if not code.isdigit() or len(code) not in LENGTHS:
        raise SupplyError("not a product barcode (8, 12, 13 or 14 digits)")
    if not gtin_check_ok(code):
        raise SupplyError("barcode check digit is wrong: scan it again")
    return code


@dataclass
class Item:
    online_variant_id: uuid.UUID
    online_product_id: uuid.UUID
    name: str
    name_si: str | None
    variant: str
    weight_grams: int | None
    image_url: str | None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def _item(db: AsyncSession, variant_id: uuid.UUID) -> Item | None:
    row = (await db.execute(
        select(OnlineVariant, OnlineProduct)
        .join(OnlineProduct, OnlineProduct.online_product_id == OnlineVariant.online_product_id)
        .where(OnlineVariant.online_variant_id == variant_id))).first()
    if row is None:
        return None
    v, p = row
    return Item(v.online_variant_id, p.online_product_id, p.name, p.name_si, v.name,
                v.weight_grams if v.weight_grams is not None else p.weight_grams, p.image_url)


async def lookup(db: AsyncSession, raw: str) -> dict:
    """{"barcode", "item"}; item is None when the barcode is not linked yet."""
    code = normalise(raw)
    row = await db.get(ProductBarcode, code)
    item = await _item(db, row.online_variant_id) if row else None
    return {"barcode": code, "item": item.as_dict() if item else None}


async def barcodes_of(db: AsyncSession, variant_id: uuid.UUID) -> list[str]:
    return list((await db.scalars(select(ProductBarcode.barcode).where(
        ProductBarcode.online_variant_id == variant_id).order_by(ProductBarcode.barcode))).all())


async def link(db: AsyncSession, actor: Actor, *, raw: str, online_variant_id: uuid.UUID,
               can_move: bool) -> dict:
    code = normalise(raw)
    item = await _item(db, online_variant_id)
    if item is None:
        raise NotFound("product not found")
    row = await db.get(ProductBarcode, code, with_for_update=True)
    now = datetime.now(UTC)
    if row is not None:
        if row.online_variant_id == online_variant_id:
            return {"barcode": code, "item": item.as_dict(), "changed": False}
        if not can_move:
            current = await _item(db, row.online_variant_id)
            raise Conflict(f"barcode {code} is already {current.name if current else 'linked'}"
                           f" {current.variant if current else ''}".rstrip()
                           + "; purchasing or the owner can move it")
        before = {"online_variant_id": str(row.online_variant_id)}
        row.online_variant_id, row.updated_by, row.updated_at = (
            online_variant_id, actor.user_id, now)
        await audit.record(db, user_id=actor.user_id, action="barcode.move",
                           entity="product_barcode", entity_id=code, before=before,
                           after={"online_variant_id": str(online_variant_id)},
                           ip_address=actor.ip_address)
    else:
        db.add(ProductBarcode(barcode=code, online_variant_id=online_variant_id,
                              created_by=actor.user_id, updated_by=actor.user_id))
        await audit.record(db, user_id=actor.user_id, action="barcode.link",
                           entity="product_barcode", entity_id=code,
                           after={"online_variant_id": str(online_variant_id)},
                           ip_address=actor.ip_address)
    await db.flush()
    return {"barcode": code, "item": item.as_dict(), "changed": True}


async def unlink(db: AsyncSession, actor: Actor, raw: str, *, allowed: bool) -> None:
    if not allowed:
        raise Forbidden("purchasing or the owner removes a barcode")
    code = normalise(raw)
    row = await db.get(ProductBarcode, code, with_for_update=True)
    if row is None:
        raise NotFound("barcode not linked")
    await audit.record(db, user_id=actor.user_id, action="barcode.unlink",
                       entity="product_barcode", entity_id=code,
                       before={"online_variant_id": str(row.online_variant_id)},
                       ip_address=actor.ip_address)
    await db.delete(row)
    await db.flush()
