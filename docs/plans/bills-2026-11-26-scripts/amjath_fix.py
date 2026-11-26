"""Owner 2026-11-26 note + photo of the Amjath goods: fix the products of stock already in
(product corrections), and enter the Bellose 720 and Vaseline 485 lines."""
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
FIXES = [  # (product the units are on now, units, right product, why)
    ("Pond's Pure Detox Charcoal Face Wash", 2, "Pond's Bright Miracle Facewash 50ml",
     "duplicate product made from the bill; the owner's product already existed"),
    ("Bellose Perfect Straight Keratin Shampoo 210ml", 1, "Bellose Perfect Curl Keratin Shampoo 210ml",
     "owner's photo: the three 890/- Bellose were Curl, Straight shampoo, Straight conditioner"),
    ("Bellose Perfect Straight Keratin Shampoo 210ml", 1,
     "Bellose Perfect Straight Keratin Conditioner 210ml",
     "owner's photo: the three 890/- Bellose were Curl, Straight shampoo, Straight conditioner"),
    ("Nature's Secrets Lotus Facial Wash 50ml", 1, "Nature's Secrets Kohomba Kaha Facial Wash 50ml",
     "owner's note E2: 10 of the 240/- kinds and 2 Kohomba (210/-)"),
    ("Nature's Secrets Papaya Facial Wash 50ml", 1, "Nature's Secrets Kohomba Kaha Facial Wash 50ml",
     "owner's note E2: 10 of the 240/- kinds and 2 Kohomba (210/-)"),
]
BELLOSE_720 = [("Bellose Hair Loss Control Shampoo 210ml", 1),  # owner: an 890/- one, billed as 720/-
               ("Bellose Anti Dandruff Shampoo 210ml", 1),
               ("Bellose Intense Repair Nourishing Shampoo 210ml", 2)]


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


async def main():
    async with get_sessionmaker()() as db:
        async with db.begin():
            print("sync", await online_catalog.sync(db, user_id=OWNER))
        tx = await db.begin()
        sup = await db.scalar(select(Supplier).where(Supplier.name == "Amjath Khan Traders"))
        branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
        for wrong, qty, right, why in FIXES:
            prod = await db.scalar(select(Product).where(Product.name == wrong))
            batch = await db.scalar(select(StockBatch).where(
                StockBatch.product_id == prod.product_id, StockBatch.supplier_id == sup.supplier_id,
                StockBatch.qty_on_hand >= qty))
            _, to = await vid(db, right)
            out = await correction.correct(db, ACTOR, batch_id=batch.batch_id, qty=Decimal(qty),
                                           to_product_id=to, reason=why)
            print(f"  {out['correction_no']}: {qty} x {wrong} -> {right} (Rs {out['value']})")

        staff = StaffActor(OWNER, "owner", None)
        # Bellose 720/- x4 (rate 540): received as listed in BELLOSE_720
        offer = await offer_of(db, sup, "Bellose shampoo/conditioner 720/-")
        lines = [quick_buy.QuickLine(online_variant_id=(await vid(db, n))[1], units=u,
                                     unit_price=offer.price, mrp=offer.mrp) for n, u in BELLOSE_720]
        offer.is_active = False
        # Vaseline 485/- x2 = Kiss Of Petals (company price 625, sold to us as 485; quick sale)
        voffer = await offer_of(db, sup, "Vaseline body lotion (S) 485/-")
        vp, vv = await vid(db, "Vaseline Kiss Of Petals Body Lotion 100ml")
        await matching.confirm(db, voffer.offer_id, online_product_id=vp, online_variant_id=vv,
                               user_id=OWNER)
        lines.append(quick_buy.QuickLine(online_variant_id=vv, units=2, unit_price=voffer.price,
                                         mrp=Decimal("625")))
        total = sum(ln.unit_price * ln.units for ln in lines)
        order, _ = await quick_buy.quick_buy(
            db, staff, actor=ACTOR, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
            received_date=date(2026, 11, 18), bill_no="18/11 bill 1 (26,318)", bill_total=total,
            notes="Part 4: Bellose 720/- line as received (owner's photo) and Vaseline 485/- = "
                  "Kiss Of Petals (company price 625, quick sale)", lines=lines)
        print(f"bill 1 part 4: {len(lines)} lines Rs {total:,.2f} -> {order.order_no}")
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
