"""Amjath lines split evenly over the owner's 11-25 products (owner MCQ 2026-11-26)."""
import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import (Branch, OnlineProduct, OnlineVariant, Supplier, SupplierOffer,
                        SupplierProduct, SupplierSection)
from app.services import quick_buy
from app.services.actors import Actor
from app.services.orders import StaffActor

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
NS = "Nature's Secrets {} Facial Wash {}"
SPLIT = {
    "18/11 bill 1 (26,318)": {
        "Nature's Secrets face wash (M) 450/-": [
            (NS.format(k, "100ml"), None) for k in
            ("Peppermint", "Carrot Soft", "Cucumber", "Kohomba Kaha", "Lotus", "Papaya")],
        "Nature's Secrets face wash (S) 240/-": [
            (NS.format(k, "50ml"), None) for k in ("Carrot Soft", "Cucumber", "Lotus", "Papaya")],
        "Dreamron shampoo/conditioner 650/-": [("Dreamron King Coconut Shampoo 100ml", None),
                                               ("Dreamron Conditioner King Coconut 100ml", None)],
        "Dreamron shampoo (S) 395/-": [("Dreamron Aloe Vera Shampoo 100ml", None),
                                       ("Dreamron Amla Extract Shampoo 100ml", None)],
    },
    "18/11 bill 2 (13,617)": {
        "Blending sponge (sell 200)": [("Liuda Beauty Makeup Blender Sponge (Powder Puff)", c)
                                       for c in ("Black", "Blue", "Pink", "White")],
        "Nail polish (S) (sell 160)": [("CR Cairuo Nail Polish 8ml", f"Shade {s}") for s in
                                       ("07", "40", "43", "45", "51", "52", "53", "68")],
        "Lipstick (sell 550)": [("Romantic Bird Matte Lipstick", f"Shade 0{s}") for s in range(1, 7)],
        "Eye brow pencil (sell 190)": [("Black Opal Eyebrow Pencil with Sharpener", None),
                                       ("Huda Beauty Waterproof Eyebrow Pencil With Sharpener", None)],
    },
}


async def variant(db, pname, vname):
    op = await db.scalar(select(OnlineProduct).where(OnlineProduct.name == pname,
                                                     OnlineProduct.is_active.is_(True)))
    assert op is not None, pname
    q = select(OnlineVariant).where(OnlineVariant.online_product_id == op.online_product_id,
                                    OnlineVariant.is_active.is_(True))
    if vname:
        q = q.where(OnlineVariant.name == vname)
    vs = list(await db.scalars(q))
    assert len(vs) == 1, (pname, vname, len(vs))
    return vs[0].online_variant_id


async def main():
    async with get_sessionmaker()() as db:
        tx = await db.begin()
        sup = await db.scalar(select(Supplier).where(Supplier.name == "Amjath Khan Traders"))
        branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
        sec = await db.scalar(select(SupplierSection).where(
            SupplierSection.supplier_id == sup.supplier_id, SupplierSection.title == "Bills to match"))
        staff = StaffActor(OWNER, "owner", None)
        actor = Actor(user_id=OWNER, role="owner", ip_address=None)
        for bill_no, lines in SPLIT.items():
            qlines, total = [], Decimal(0)
            for heading, targets in lines.items():
                prod = await db.scalar(select(SupplierProduct).where(
                    SupplierProduct.section_id == sec.section_id, SupplierProduct.heading == heading))
                offer = await db.scalar(select(SupplierOffer).where(
                    SupplierOffer.product_id == prod.product_id,
                    SupplierOffer.label.like(f"{bill_no} · %")))
                qty = int(offer.label.rsplit("· ", 1)[1].split()[0])
                each, rest = divmod(qty, len(targets))
                assert rest == 0, heading
                for pname, vname in targets:
                    qlines.append(quick_buy.QuickLine(
                        online_variant_id=await variant(db, pname, vname), units=each,
                        unit_price=offer.price, mrp=offer.mrp))
                    total += offer.price * each
                offer.is_active = False  # entered: off the matching queue
                print(f"  {heading}: {qty} = {len(targets)} x {each} @ {offer.price}")
            order, _ = await quick_buy.quick_buy(
                db, staff, actor, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
                received_date=date(2026, 11, 18), bill_no=bill_no, bill_total=total,
                notes="Part 3 of the bill: split evenly over the owner's products (owner 2026-11-26)",
                lines=qlines)
            print(f"{bill_no}: {len(qlines)} lines Rs {total:,.2f} -> {order.order_no}")
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
