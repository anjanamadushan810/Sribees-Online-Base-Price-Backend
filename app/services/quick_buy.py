"""Quick buy (decision 21; owner 2026-11-26): goods bought for cash at the market go into
stock in one step, through Supply like every other buy.

One call makes the order for a supplier without the app (the market shop), with one line
per item at the price paid; submits it (Finance gets its PO and approval as for any
order, after the fact); and receives the goods at once with their labels, expiry and the
bill's number and total. Finance then matches the bill (receipt.posted) and the accountant
records the cash payment there.

Each line names one of OUR pack sizes (owner 2026-11-26: we order and receive in our own
product names; what the shop's bill calls it does not matter). Its price is the base price.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BasePrice,
    OnlineProduct,
    OnlineVariant,
    Supplier,
    SupplyOrderLine,
)
from app.services import orders, receiving
from app.services.actors import Actor
from app.services.errors import NotFound, SupplyError

SECTION = "Quick buys"
MAX_LINES = 200


@dataclass(frozen=True)
class QuickLine:
    online_variant_id: uuid.UUID
    units: int
    unit_price: Decimal
    free_units: int = 0
    weight_kg: Decimal | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = None
    labels: tuple[str, ...] = field(default_factory=tuple)


async def search_items(db: AsyncSession, q: str, limit: int = 30) -> list[dict]:
    """Online pack sizes by product name (English or Sinhala), SKU or variant SKU."""
    q = q.strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    rows = (await db.execute(
        select(OnlineVariant, OnlineProduct)
        .join(OnlineProduct, OnlineProduct.online_product_id == OnlineVariant.online_product_id)
        .where(OnlineProduct.is_active.is_(True), OnlineVariant.is_active.is_(True),
               OnlineProduct.name.ilike(like) | OnlineProduct.name_si.ilike(like)
               | (OnlineProduct.sku == q) | (OnlineVariant.sku == q))
        .order_by(OnlineProduct.name, OnlineVariant.name).limit(limit))).all()
    out = []
    for v, p in rows:
        last = await db.scalar(select(BasePrice.unit_price).where(
            BasePrice.online_variant_id == v.online_variant_id, BasePrice.voided_at.is_(None))
            .order_by(BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc()).limit(1))
        out.append({"online_variant_id": v.online_variant_id,
                    "online_product_id": p.online_product_id, "name": p.name,
                    "name_si": p.name_si, "variant": v.name, "weight_grams": v.weight_grams,
                    "image_url": p.image_url,
                    "last_price": f"{last:.2f}" if last is not None else None})
    return out


async def quick_buy(db: AsyncSession, staff: orders.StaffActor, actor: Actor, *,
                    supplier_id: uuid.UUID, branch_id: uuid.UUID, received_date: date,
                    bill_no: str | None, bill_total: Decimal | None, notes: str | None,
                    lines: list[QuickLine]):
    if not (staff.can("orders.create") and staff.can("grn.create")):
        raise SupplyError("purchasing or the owner records a quick buy")
    if not lines:
        raise SupplyError("add at least one item")
    if len(lines) > MAX_LINES:
        raise SupplyError(f"at most {MAX_LINES} items in one quick buy")
    if len({ln.online_variant_id for ln in lines}) != len(lines):
        raise SupplyError("an item appears twice; put its units on one line")
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise NotFound("supplier not found")
    # A supplier on the app may be paid at their counter too (owner 2026-11-26: Nishi
    # Palace bills already paid in cash); the order is received at once, so the app never
    # shows it to them.

    order = await orders.create(db, staff, supplier_id=supplier_id, branch_id=branch_id,
                                note=("Quick buy" + (f": {notes}" if notes else ""))[:2000])
    order.is_quick_buy = True
    line_of: dict[uuid.UUID, SupplyOrderLine] = {}
    for n, ln in enumerate(lines, start=1):
        if not (0 < ln.units <= receiving.MAX_UNITS) or ln.unit_price < 0:
            raise SupplyError(f"item {n}: units above zero and a price, please")
        # our own pack size, never the shop's name for it (owner 2026-11-26)
        order = await orders.add_line(db, staff, order.order_id,
                                      online_variant_id=ln.online_variant_id, units=ln.units,
                                      unit_price=ln.unit_price)
        row = next(x for x in order.lines if x.online_variant_id == ln.online_variant_id)
        row.mrp = ln.mrp
        line_of[ln.online_variant_id] = row
    await db.refresh(order, ["lines"])
    await orders.submit(db, staff, order.order_id)  # Finance's PO + approval, as usual
    order.approved_by_name = order.approved_by_name or "Quick buy (paid at the market)"
    receipt = await receiving.receive(
        db, actor, order.order_id, received_date=received_date, invoice_no=bill_no,
        invoice_total=bill_total, notes=notes,
        lines=[receiving.ReceiptLineIn(
            order_line_id=line_of[ln.online_variant_id].line_id, units=ln.units,
            free_units=ln.free_units, weight_kg=ln.weight_kg, expiry_date=ln.expiry_date,
            mfg_date=ln.mfg_date, mrp=ln.mrp, product_id=ln.online_variant_id,
            labels=tuple(ln.labels)) for ln in lines],
        scope=staff.branch_scope)
    return order, receipt
