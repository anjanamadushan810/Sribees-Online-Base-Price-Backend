"""Amjath lines whose Online product is certain by the owner's 11-25 products (name + price):
confirm the match in the queue (as the owner) and receive them as quick buys."""
import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import (Branch, OnlineProduct, OnlineVariant, Supplier, SupplierOffer,
                        SupplierProduct, SupplierSection)
from app.services import matching, quick_buy
from app.services.actors import Actor
from app.services.orders import StaffActor

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
SURE = {  # bill line -> Online product (owner's 11-25 product at the bill's MRP)
    "18/11 bill 1 (26,318)": {
        "Chandanalepa cream 250/-": "Chandanalepa Ayurvedic Herbal Face Cream 12g",
        "Chandanalepa cream 340/-": "Chandanalepa Ayurvedic Herbal Face Cream 20g",
        "Chandanalepa soap (S) 220/-": "Chandanalepa Kohomba Ayurveda Soap 75g",
        "Chandanalepa soap (L) 350/-": "Chandanalepa Soap Herbal 100g",
        "Bellose shampoo/conditioner 890/-": "Bellose Perfect Straight Keratin Shampoo 210ml",
        "Shello body lotion (S) 340/-": "Shello Fairness Lotion 125ml",
        "Vaseline body lotion (R) 450/-": "Vaseline Aloe Fresh Body Lotion 100ml",
        "Vaseline body lotion (S) 600/-": "Vaseline Pretty As A Peach Body Lotion 100ml",
        "Vaseline body lotion (L) 940/-": "Vaseline Healthy Bright Body Lotion 200ml",
        "Pond's pink face wash 270/-": "Pond's Bright Beauty Daily Brightening Facewash 50ml",
    },
    "18/11 bill 2 (13,617)": {
        "Pond's gel moisturiser 470/-": "Pond's Hydrated Glow Super Light Gel 25g",
        "Pond's moisturiser 400/-": "Pond's Light Moisturizer 25ml",
        "Fair & Lovely cream (S) 385/-": "Glow & Lovely Advanced Multi Vitamin Face Cream 50g",
        "Extractor pad (sell 120)": "Blackhead Remover Tool Acne Pimple Spot Extractor Pin",
        "Miss Rose pen eyeliner (sell 550)": "Miss Rose Liquid Eyeliner",
    },
}


async def main():
    async with get_sessionmaker()() as db:
        tx = await db.begin()
        sup = await db.scalar(select(Supplier).where(Supplier.name == "Amjath Khan Traders"))
        branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
        sec = await db.scalar(select(SupplierSection).where(
            SupplierSection.supplier_id == sup.supplier_id, SupplierSection.title == "Bills to match"))
        staff = StaffActor(OWNER, "owner", None)
        actor = Actor(user_id=OWNER, role="owner", ip_address=None)
        for bill_no, lines in SURE.items():
            qlines, total = [], Decimal(0)
            for heading, pname in lines.items():
                prod = await db.scalar(select(SupplierProduct).where(
                    SupplierProduct.section_id == sec.section_id, SupplierProduct.heading == heading))
                offers = list(await db.scalars(select(SupplierOffer).where(
                    SupplierOffer.product_id == prod.product_id,
                    SupplierOffer.label.like(f"{bill_no} · %"))))
                assert len(offers) == 1, heading
                offer = offers[0]
                qty = int(offer.label.rsplit("· ", 1)[1].split()[0])
                op = await db.scalar(select(OnlineProduct).where(OnlineProduct.name == pname,
                                                                 OnlineProduct.is_active.is_(True)))
                v = await db.scalar(select(OnlineVariant).where(
                    OnlineVariant.online_product_id == op.online_product_id,
                    OnlineVariant.is_active.is_(True)))
                await matching.confirm(db, offer.offer_id, online_product_id=op.online_product_id,
                                       online_variant_id=v.online_variant_id, user_id=OWNER)
                qlines.append(quick_buy.QuickLine(online_variant_id=v.online_variant_id, units=qty,
                                                  unit_price=offer.price, mrp=offer.mrp))
                total += offer.price * qty
                print(f"  {heading} -> {pname}: {qty} x {offer.price}")
            order, _ = await quick_buy.quick_buy(
                db, staff, actor, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
                received_date=date(2026, 11, 18), bill_no=bill_no, bill_total=total,
                notes="Part 2 of the bill: lines matched by the owner's products (name + price)",
                lines=qlines)
            print(f"{bill_no}: {len(qlines)} lines Rs {total:,.2f} -> {order.order_no}")
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
