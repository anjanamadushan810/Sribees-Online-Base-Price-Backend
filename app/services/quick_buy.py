"""Quick buy (decision 21; owner 2026-11-26): goods bought for cash at the market go into
stock in one step, through Supply like every other buy.

One call makes the order for a supplier without the app (the market shop), with one line
per item at the price paid; submits it (Finance gets its PO and approval as for any
order, after the fact); and receives the goods at once with their labels, expiry and the
bill's number and total. Finance then matches the bill (receipt.posted) and the accountant
records the cash payment there.

Each line becomes an offer in the supplier's "Quick buys" section (an order line names an
offer); the shop has no app, so nobody else sees that list.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    OfferMatch,
    OnlineProduct,
    OnlineVariant,
    Supplier,
    SupplierOffer,
    SupplierProduct,
    SupplierSection,
    SupplierUser,
    SupplyOrderLine,
)
from app.services import orders, receiving
from app.services.actors import Actor
from app.services.errors import Conflict, NotFound, SupplyError

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
    return [{"online_variant_id": v.online_variant_id, "online_product_id": p.online_product_id,
             "name": p.name, "name_si": p.name_si, "variant": v.name,
             "weight_grams": v.weight_grams} for v, p in rows]


async def _section(db: AsyncSession, supplier_id: uuid.UUID) -> SupplierSection:
    section = await db.scalar(select(SupplierSection).where(
        SupplierSection.supplier_id == supplier_id,
        func.lower(SupplierSection.title) == SECTION.lower()))
    if section is None:
        section = SupplierSection(supplier_id=supplier_id, title=SECTION)
        db.add(section)
        await db.flush()
    return section


async def _offer(db: AsyncSession, supplier_id: uuid.UUID, section: SupplierSection,
                 heading: str, label: str, price: Decimal, mrp: Decimal | None) -> SupplierOffer:
    product = await db.scalar(select(SupplierProduct).where(
        SupplierProduct.supplier_id == supplier_id,
        SupplierProduct.section_id == section.section_id, SupplierProduct.heading == heading))
    if product is None:
        product = SupplierProduct(supplier_id=supplier_id, section_id=section.section_id,
                                  heading=heading)
        db.add(product)
        await db.flush()
    offer = SupplierOffer(supplier_id=supplier_id, product_id=product.product_id, label=label,
                          price=price, mrp=mrp, units_per_box=1)
    db.add(offer)
    await db.flush()
    return offer


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
    if await db.scalar(select(SupplierUser.supplier_user_id).where(
            SupplierUser.supplier_id == supplier_id, SupplierUser.is_active.is_(True)).limit(1)):
        raise Conflict(f"{supplier.name} uses the supplier app: order from them the usual way")

    order = await orders.create(db, staff, supplier_id=supplier_id, branch_id=branch_id,
                                note=("Quick buy" + (f": {notes}" if notes else ""))[:2000])
    order.is_quick_buy = True
    section = await _section(db, supplier_id)
    line_of: dict[uuid.UUID, SupplyOrderLine] = {}
    for n, ln in enumerate(lines, start=1):
        variant = await db.get(OnlineVariant, ln.online_variant_id)
        if variant is None or not variant.is_active:
            raise SupplyError(f"item {n} is not an Online pack size; sync the catalogue")
        product = await db.get(OnlineProduct, variant.online_product_id)
        if not (0 < ln.units <= receiving.MAX_UNITS) or ln.unit_price < 0:
            raise SupplyError(f"{product.name}: units above zero and a price, please")
        heading = product.name[:200]
        offer = await _offer(db, supplier_id, section, heading, (variant.name or "")[:120],
                             ln.unit_price, ln.mrp)
        # staff chose the Online pack size themselves: the match is confirmed by them
        db.add(OfferMatch(offer_id=offer.offer_id, supplier_id=supplier_id,
                          online_product_id=product.online_product_id,
                          online_variant_id=variant.online_variant_id, status="confirmed",
                          decided_by=staff.user_id, decided_at=datetime.now(UTC)))
        row = SupplyOrderLine(order_id=order.order_id, supplier_id=supplier_id,
                              offer_id=offer.offer_id, online_product_id=product.online_product_id,
                              online_variant_id=variant.online_variant_id, heading=heading,
                              label=offer.label, boxes=ln.units, units_per_box=1,
                              unit_price=ln.unit_price, mrp=ln.mrp)
        db.add(row)
        await db.flush()
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
