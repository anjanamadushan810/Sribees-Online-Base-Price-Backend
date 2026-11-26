"""Enter the market bills as Quick buys on Supply prod (owner 2026-11-26). Run inside the
api container: python - [commit] < enter_bills.py. Without "commit" it rolls back."""
import asyncio
import sys
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models import Branch, OnlineProduct, OnlineVariant, Supplier
from app.services import online_catalog, quick_buy, suppliers
from app.services.actors import Actor
from app.services.orders import StaffActor

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
C = Decimal("0.01")

NEW_SUPPLIERS = {
    "Amjath Khan Traders": dict(
        notes="Wholesale and retail cosmetics and salon accessories. Card 2026-11-26.",
        details={"phone": "+94777634857", "email": "h.amjathkhan2010@gmail.com",
                 "address": "No. 183, 1/1, Main Street, Colombo 11. "
                            "Also +94 77 363 4147, 011 233 7721"}),
    "Ceylon Biscuits Limited (Munchee)": dict(
        notes="Munchee. Sales officer Dilanka Sandakel. Bills come from the distributor "
              "New Wijesiri Stores, No 04 Ambagaha Junction, Dharga Town, 034 227 4825.",
        details={"contact_name": "Dilanka Sandakel", "phone": "+94762458475",
                 "address": "No. 555, High-Level Road, Makumbura, Pannipitiya. Tel 011 500 0000"}),
}

# (online product name, units, unit price, MRP, free units)
NISHI_A = [
    ("Baby Cheramy Soap S-Pack +1 Free", 36, "574", "710", 0),
    ("Calin Purple Soap 70g", 144, "97", "125", 0),
    ("Clogard Toothpaste 120g", 48, "199", "240", 0),
    ("Clogard Toothpaste 70g", 60, "169", "205", 0),
    ("Coca Cola Orginal Taste 1050ml", 12, "269", "320", 0),
    ("Coca Cola Orginal Taste 250ml", 30, "98", "120", 0),
    ("Coca Cola 400ml", 24, "179", "200", 0),
    ("Diva Washing Powder 1kg", 15, "299", "375", 0),
    ("Lifebuoy Soap Double Pack", 75, "239", "280", 0),
    ("Pears Pure & Gentle Baby Soap 70g", 162, "133", "165", 0),
    ("Rani Sandalwood Soap 70g", 144, "104", "135", 0),
    ("Sprite Lemon Lime Flavoured 1050ml", 12, "269", "320", 0),
    ("Sprite Lemon Lime Flavoured 250ml", 30, "98", "120", 0),
    ("Sprite Lemon Lime Flavoured 400ml", 24, "179", "200", 0),
    ("Sunlight With Real Lemon Power 100g×4", 36, "444", "500", 0),
    ("Sunlight Detergent Powder Clean & Citrus 1kg", 18, "309", "375", 0),
    ("Vim Dishwash Bar 100g", 144, "56", "70", 0),
    ("Vim Dishwash Liquid 500ml", 24, "389", "450", 0),
]
NISHI_B = [
    ("Anchor Full Cream Milk Powder 400g", 36, "1244", "1300", 0),
    ("Fems Sanitary Napkin Pink", 24, "184", "270", 0),
    ("Happy Cow Cheese 8 Portions", 48, "788", "880", 0),
    ("Maliban Chocolate Biscuit", 50, "114", "140", 0),
    ("Pelwatte Full Cream Milk Powder 400g", 36, "1099", "1200", 0),
    ("Prima Instant Noodles 325g", 24, "344", "390", 0),
    ("Ratthi Full Cream Milk Powder 400g", 36, "1244", "1300", 0),
    ("Samaposha 200g", 25, "168", "200", 0),
    ("Samaposha 500g", 12, "424", "475", 0),
    ("Signal Triple Protection 123 Toothpaste 120g", 48, "218", "255", 0),
    ("Signal Toothpaste 160g + Free Brush", 48, "285", "305", 0),
    ("Signal Toothpaste 200g", 48, "284", "365", 0),
    ("Signal Toothpaste 70g", 72, "186", "205", 0),
    ("Viva Malted Food Drink 400g", 24, "699", "780", 0),
    ("Watawala Tea 50g", 240, "128", "160", 0),
]
NISHI_C = [
    ("Fems Sanitary Napkin Pink", 72, "184", "270", 0),
    ("Sprite Lemon Lime Flavoured 250ml", 60, "98", "120", 0),
    ("Sprite Lemon Lime Flavoured 400ml", 48, "174", "200", 0),
    ("Sprite Lemon Lime Flavoured 1050ml", 24, "269", "320", 0),
    ("Coca Cola Orginal Taste 250ml", 60, "98", "120", 0),
    ("Coca Cola 400ml", 24, "174", "200", 0),
    ("Coca Cola Orginal Taste 1050ml", 12, "269", "320", 0),
]
AMJATH_D = [("Pond's Pure Detox Charcoal Face Wash", 2, "220", "250", 0)]
AMJATH_E = [
    ("Dulhan Waterproof Eyeliner With Mirror (Bottled)", 6, "175", None, 0),
    ("3 Pcs Tinkle Eyebrow Razor Set", 3, "90", None, 0),
    ("Lucky Puff Make-Up Sponge Powder Puff 1pc", 3, "175", None, 0),
    ("Pumice Stone", 3, "125", None, 0),
]
# Munchee 56096: bill rate x (net 104,381.24 / gross 108,721.59) = the price really paid
F96 = Decimal("104381.24") / Decimal("108721.59")
MUNCHEE_96 = [
    ("Munchee Onion Biscuits 30g", 24, "45", "50", 0),
    ("Munchee Cheese Button 20g", 24, "45", "50", 0),
    ("Munchee Savoury Nuts 30g", 24, "45", "50", 0),
    ("Munchee Snak Cracker 30g", 24, "45", "50", 0),
    ("Munchee Super Cream Cracker 490g", 45, "400.50", "450", 3),
    ("Munchee Super Cream Cracker 125g", 24, "126", "140", 0),
    ("Munchee Super Cream Cracker 190g", 12, "180", "200", 0),
    ("Munchee Super Cream Cracker Handy Pack 85g", 24, "90", "100", 0),
    ("Munchee Cheese Cracker 200g", 12, "261", "290", 0),
    ("Munchee Potato Cracker 110g", 12, "171", "190", 0),
    ("Munchee Sun Cracker 95g", 12, "108", "120", 0),
    ("Munchee Lemon Puff 100g", 24, "126", "140", 0),
    ("Munchee Lemon Puff 200g", 15, "243", "270", 0),
    ("Munchee chocolate puff 100g", 24, "126", "140", 0),
    ("Munchee Chocolate Puff 200g", 15, "243", "270", 0),
    ("Munchee Tikiri Marie 80g", 60, "78.75", "90", 0),
    ("Munchee Chocolate Marie 90g", 24, "90", "100", 0),
    ("Munchee Lite Marie 50g", 24, "45", "50", 0),
    ("Munchee Chocolate Cream 100g", 25, "121.80", "140", 0),
    ("Munchee Nice Biscuits 100g", 15, "106.20", "120", 0),
    ("Munchee Nice 200g", 6, "212.40", "240", 0),
    ("Munchee Milk Cream Biscuits 110g", 12, "117", "130", 0),
    ("Munchee Choc Cream Biscuits 88g", 12, "99", "110", 0),
    ("Munchee Chocolate Cream Biscuits 210g", 12, "226.20", "260", 0),
    ("Munchee Ginger Biscuits 200g", 6, "221.25", "250", 0),
    ("Munchee Chocolate Marie 200g", 6, "207", "230", 0),
    ("Munchee Tifin 125g", 12, "180", "200", 0),
    ("Munchee Hawaian Cookies 100g", 12, "126", "140", 0),
    ("Munchee Hawaian Cookies 200g", 15, "234", "260", 0),
    ("Munchee Milk Short Cake Biscuits 85g", 15, "108", "120", 0),
    ("Munchee Milk Short Cake 200g", 12, "225", "250", 0),
    ("Munchee Tikiri Marie 360g", 4, "306.25", "350", 0),
    ("Munchee Nice Biscuits 400g", 4, "407.10", "460", 0),
    ("CBL Munchee Stix Rolled Wafer Vanilla Flavoured 20g", 24, "35.20", "40", 0),
]
F93 = Decimal("16588.80") / Decimal("17280.00")
MUNCHEE_93 = [("Munchee Super Cream Cracker Handy Pack 230g", 80, "216", "240", 5)]

BILLS = [
    ("Nishi Super Palace", date(2026, 11, 22), "26-200-001340", NISHI_A, 1,
     "Printed lines only; Velvet, Clogard 40g, Spinner, Rasmi follow through matching"),
    ("Nishi Super Palace", date(2026, 11, 22), "26-200-001341", NISHI_B, 1,
     "Meliban milk 400g struck out (not received); Aya, canned fish, Clogard 200g, Eva, "
     "Nestomalt, Signal 40g follow through matching"),
    ("Nishi Super Palace", date(2026, 11, 22), "HANDWRITTEN-63492", NISHI_C, 1,
     "Handwritten bill, no date (owner: Nishi, 22/11); Aya 144 follows through matching"),
    ("Amjath Khan Traders", date(2026, 11, 18), "18/11 bill 1 (26,318)", AMJATH_D, 1,
     "Part of the bill; the rest follows through matching"),
    ("Amjath Khan Traders", date(2026, 11, 18), "18/11 bill 2 (13,617)", AMJATH_E, 1,
     "Part of the bill; the rest follows through matching"),
    ("Ceylon Biscuits Limited (Munchee)", date(2026, 11, 25), "56096", MUNCHEE_96, F96,
     "New Wijesiri Stores bill, net 104,381.24 after discount (prices = rate less 3.99%); "
     "lines not on Online follow through matching"),
    ("Ceylon Biscuits Limited (Munchee)", date(2026, 11, 25), "56093", MUNCHEE_93, F93,
     "New Wijesiri Stores bill, net 16,588.80 after 4% discount"),
]


async def variant_of(db, name):
    ps = list(await db.scalars(select(OnlineProduct).where(
        OnlineProduct.name == name, OnlineProduct.is_active.is_(True))))
    if len(ps) != 1:
        raise SystemExit(f"product not found once: {name!r} ({len(ps)})")
    vs = list(await db.scalars(select(OnlineVariant).where(
        OnlineVariant.online_product_id == ps[0].online_product_id,
        OnlineVariant.is_active.is_(True))))
    if len(vs) != 1:
        raise SystemExit(f"{name}: {len(vs)} active pack sizes")
    return vs[0].online_variant_id


async def main():
    async with get_sessionmaker()() as db:
        async with db.begin():
            print("sync", await online_catalog.sync(db, user_id=OWNER))
        tx = await db.begin()
        try:
            branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
            for name, spec in NEW_SUPPLIERS.items():
                if not await db.scalar(select(Supplier).where(func.lower(Supplier.name) == name.lower())):
                    s = await suppliers.create(db, name=name, notes=spec["notes"], user_id=OWNER,
                                               details=spec["details"])
                    print("supplier", s.code, name)
            staff = StaffActor(OWNER, "owner", None)
            actor = Actor(user_id=OWNER, role="owner", ip_address=None)
            grand = Decimal(0)
            for sname, day, bill_no, rows, factor, note in BILLS:
                sup = await db.scalar(select(Supplier).where(Supplier.name == sname))
                lines, total = [], Decimal(0)
                for pname, units, price, mrp, free in rows:
                    paid = (Decimal(price) * factor).quantize(C, ROUND_HALF_UP)
                    total += paid * units
                    lines.append(quick_buy.QuickLine(
                        online_variant_id=await variant_of(db, pname), units=units,
                        unit_price=paid, free_units=free, mrp=Decimal(mrp) if mrp else None))
                order, receipt = await quick_buy.quick_buy(
                    db, staff, actor, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
                    received_date=day, bill_no=bill_no[:60], bill_total=total, notes=note,
                    lines=lines)
                grand += total
                print(f"{sname} {bill_no}: {len(lines)} lines, Rs {total:,.2f} -> "
                      f"{order.order_no} {order.status}")
            print(f"TOTAL Rs {grand:,.2f}")
            if COMMIT:
                await tx.commit()
                print("COMMITTED")
            else:
                await tx.rollback()
                print("dry run: rolled back")
        except BaseException:
            await tx.rollback()
            raise


asyncio.run(main())
