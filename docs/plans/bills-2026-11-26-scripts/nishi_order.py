"""Owner 2026-11-27: ONE Nishi order in our product names (bills 001340, 001341 and the
handwritten one, all 2026-11-22), to be received by staff with QR labels. Lines whose kind the
bill did not say are best guesses; staff correct them at receiving."""
import asyncio
import sys
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import Branch, OnlineProduct, OnlineVariant, Supplier, SupplyOrder
from app.services import orders

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
# (Online product, units, price per unit, MRP); several bill lines of one item are merged
LINES = [
    ("Baby Cheramy Soap S-Pack +1 Free", 36, "574", "710"),
    ("Calin Purple Soap 70g", 144, "97", "125"),
    ("Clogard Toothpaste 120g", 48, "199", "240"),
    ("Clogard Toothpaste 70g", 60, "169", "205"),
    ("Clogard Mint Flavor 40g", 60, "97", "130"),                       # guess (Mint/Cavity)
    ("Coca Cola Orginal Taste 1050ml", 24, "269", "320"),
    ("Coca Cola Orginal Taste 250ml", 90, "98", "120"),
    ("Coca Cola 400ml", 48, "176.50", "200"),                          # 24 x 179 + 24 x 174
    ("Diva Washing Powder 1kg", 15, "299", "375"),
    ("Lifebuoy Soap Double Pack", 75, "239", "280"),
    ("Pears Pure & Gentle Baby Soap 70g", 162, "133", "165"),
    ("Rani Sandalwood Soap 70g", 144, "104", "135"),
    ("Sprite Lemon Lime Flavoured 1050ml", 36, "269", "320"),
    ("Sprite Lemon Lime Flavoured 250ml", 90, "98", "120"),
    ("Sprite Lemon Lime Flavoured 400ml", 72, "175.67", "200"),        # 24 x 179 + 48 x 174
    ("Sunlight With Real Lemon Power 100g×4", 36, "444", "500"),
    ("Sunlight Detergent Powder Clean & Citrus 1kg", 18, "309", "375"),
    ("Velvet Milk And Almond 70g", 45, "90", "120"),                    # 180 split, guess
    ("Velvet Rose Petals And Milk 70g", 45, "90", "120"),
    ("Velvet Rosehip And Creamy Peach 70g", 45, "90", "120"),
    ("Velvet Sandal And Curd 70g", 45, "90", "120"),
    ("Vim Dishwash Bar 100g", 144, "56", "70"),
    ("Vim Dishwash Liquid 500ml", 24, "389", "450"),
    ("Anchor Full Cream Milk Powder 400g", 36, "1244", "1300"),
    ("Fems Sanitary Napkin Pink", 96, "184", "270"),                   # 24 + 72
    ("Fems Aya Cotton Feel Wings Napkin 8 Sanitary Napkins", 84, "113", "150"),  # 168 split
    ("Fems Aya Drytex Wings Napkin 8 Sanitary Napkins", 84, "113", "150"),
    ("Sakwa Jack Mackerel Canned Fish 425g", 24, "449", "560"),        # guess (brand)
    ("Clogard Mega Value Family Pack 200g", 24, "256", "299"),         # guess (promo 200g)
    ("Eva Cotton Feel Wings 10+2 Pads", 24, "199", "270"),             # guess
    ("Happy Cow Cheese 8 Portions", 48, "788", "880"),
    ("Maliban Chocolate Biscuit", 50, "114", "140"),
    ("Nestomalt Malted Beverage Pouch 400g", 40, "709", "780"),        # guess (pouch/box)
    ("Pelwatte Full Cream Milk Powder 400g", 36, "1099", "1200"),
    ("Prima Instant Noodles 325g", 24, "344", "390"),
    ("Ratthi Full Cream Milk Powder 400g", 36, "1244", "1300"),
    ("Samaposha 200g", 25, "168", "200"),
    ("Samaposha 500g", 12, "424", "475"),
    ("Signal Triple Protection 123 Toothpaste 120g", 48, "218", "255"),
    ("Signal Toothpaste 160g + Free Brush", 48, "285", "305"),
    ("Signal Toothpaste 200g", 48, "284", "365"),
    ("Signal Toothpaste 70g", 72, "186", "205"),
    ("Signal Herbal Toothpaste 40g", 36, "111", "135"),                # 72 split, guess
    ("Signal Toothpaste Strong Teeth 40g", 36, "111", "135"),
    ("Viva Malted Food Drink 400g", 24, "699", "780"),
    ("Watawala Tea 50g", 240, "128", "160"),
]
DAY = datetime.fromisoformat("2026-11-22T10:00:00+05:30")


async def main():
    async with get_sessionmaker()() as db:
        tx = await db.begin()
        sup = await db.scalar(select(Supplier).where(Supplier.name == "Nishi Super Palace"))
        branch = await db.scalar(select(Branch).where(Branch.name == "Matugama"))
        staff = orders.StaffActor(OWNER, "owner", None)
        order = await orders.create(
            db, staff, supplier_id=sup.supplier_id, branch_id=branch.branch_id,
            note=("Nishi Palace 22/11: bills 26-200-001340, 26-200-001341 and the handwritten "
                  "one, in our product names. Lines marked as guesses (kind/brand) are corrected "
                  "at receiving. Not on Online yet: Ride/Spinner drink x24, Rashmi matchboxes."))
        total = Decimal(0)
        for name, units, price, mrp in LINES:
            op = await db.scalar(select(OnlineProduct).where(OnlineProduct.name == name,
                                                             OnlineProduct.is_active.is_(True)))
            assert op is not None, name
            vs = list(await db.scalars(select(OnlineVariant).where(
                OnlineVariant.online_product_id == op.online_product_id,
                OnlineVariant.is_active.is_(True))))
            assert len(vs) == 1, (name, len(vs))
            order = await orders.add_line(db, staff, order.order_id,
                                          online_variant_id=vs[0].online_variant_id,
                                          units=units, unit_price=Decimal(price))
            ln = next(x for x in order.lines if x.online_variant_id == vs[0].online_variant_id)
            ln.mrp = Decimal(mrp)
            total += (Decimal(price) * units).quantize(Decimal("0.01"), ROUND_HALF_UP)
        order.is_quick_buy = True  # paid at their counter: no supplier acceptance needed
        await db.flush()
        order = await orders.submit(db, staff, order.order_id)
        order.created_at = DAY  # the bills' date (owner: the real date)
        order.submitted_at = DAY
        await db.flush()
        print(f"{order.order_no}: {len(LINES)} lines, Rs {total:,.2f}, status {order.status}")
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
