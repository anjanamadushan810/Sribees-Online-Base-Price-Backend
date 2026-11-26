"""Owner 2026-11-26 photos: the last Amjath lines (Shello 320 x5, Shello 350 x1, Vaseline 490 x3)."""
import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import (Branch, OnlineProduct, OnlineVariant, Product, StockBatch, Supplier,
                        SupplierOffer, SupplierProduct, SupplierSection)
from app.services import correction, matching, online_catalog, quick_buy
from app.services.actors import Actor
from app.services.orders import StaffActor

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
ACTOR = Actor(user_id=OWNER, role="owner", ip_address=None)
async def vid(db, name):
    op = await db.scalar(select(OnlineProduct).where(OnlineProduct.name == name,
                                                     OnlineProduct.is_active.is_(True)))
    assert op is not None, name
    v = list(await db.scalars(select(OnlineVariant).where(
        OnlineVariant.online_product_id == op.online_product_id, OnlineVariant.is_active.is_(True))))
    assert len(v) == 1, name
    return op.online_product_id, v[0].online_variant_id


async def offer_of(db, sup, heading):
    sec = await db.scalar(select(SupplierSection).where(
        SupplierSection.supplier_id == sup.supplier_id, SupplierSection.title == "Bills to match"))
    prod = await db.scalar(select(SupplierProduct).where(
        SupplierProduct.section_id == sec.section_id, SupplierProduct.heading == heading))
    return await db.scalar(select(SupplierOffer).where(SupplierOffer.product_id == prod.product_id))


LINES = {  # bill line -> [(Online product, units)]
    "Shello body lotion (S) 320/-": [("Shello Aloe Vera Lotion 125ml", 2),
                                     ("Shello Moisturising Lotion 125ml", 2),
                                     ("Shello Vitamin E Body Lotion 125ml", 1)],
    "Shello body lotion (S) 350/-": [("Shello Herbal Beauty Body Lotion 125ml", 1)],
    "Vaseline body lotion (S) 490/-": [("Vaseline Healthy Bright Body Lotion 100ml", 3)],
}


async def main():
    async with get_sessionmaker()() as db:
        async with db.begin():
            print("sync", await online_catalog.sync(db, user_id=OWNER))
        tx = await db.begin()
        sup = await db.scalar(select(Supplier).where(Supplier.name == "Amjath Khan Traders"))
        branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
        staff = StaffActor(OWNER, "owner", None)
        lines = []
        for heading, targets in LINES.items():
            offer = await offer_of(db, sup, heading)
            qty = int(offer.label.rsplit("· ", 1)[1].split()[0])
            assert qty == sum(u for _, u in targets), heading
            if len(targets) == 1:
                p, v = await vid(db, targets[0][0])
                await matching.confirm(db, offer.offer_id, online_product_id=p,
                                       online_variant_id=v, user_id=OWNER)
            else:
                offer.is_active = False  # split over kinds: off the queue once entered
            for name, units in targets:
                lines.append(quick_buy.QuickLine(online_variant_id=(await vid(db, name))[1],
                                                 units=units, unit_price=offer.price, mrp=offer.mrp))
        total = sum(ln.unit_price * ln.units for ln in lines)
        order, _ = await quick_buy.quick_buy(
            db, staff, actor=ACTOR, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
            received_date=date(2026, 11, 18), bill_no="18/11 bill 1 (26,318)", bill_total=total,
            notes="Part 5: Shello and Vaseline lines from the owner's photos", lines=lines)
        print(f"bill 1 part 5: {len(lines)} lines Rs {total:,.2f} -> {order.order_no}")
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
