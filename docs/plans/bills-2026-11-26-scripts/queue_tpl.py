import asyncio, json, sys
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import func, select
from app.core.db import get_sessionmaker
from app.models import Supplier, SupplierOffer, SupplierProduct, SupplierSection
from app.services import matching

DATA = json.loads(r'''__DATA__''')
COMMIT = "commit" in sys.argv

def factor(f):
    a, _, b = f.partition("/")
    return Decimal(a) / Decimal(b) if b else Decimal(a)

async def main():
    async with get_sessionmaker()() as db:
        tx = await db.begin()
        made, sups = 0, set()
        for bill in DATA["bills"]:
            sup = await db.scalar(select(Supplier).where(Supplier.name == bill["supplier"]))
            sups.add(sup.supplier_id)
            sec = await db.scalar(select(SupplierSection).where(
                SupplierSection.supplier_id == sup.supplier_id,
                func.lower(SupplierSection.title) == DATA["section"].lower()))
            if sec is None:
                sec = SupplierSection(supplier_id=sup.supplier_id, title=DATA["section"])
                db.add(sec); await db.flush()
            f = factor(bill["factor"])
            for text, qty, rate, mrp, free in bill["lines"]:
                prod = await db.scalar(select(SupplierProduct).where(
                    SupplierProduct.supplier_id == sup.supplier_id,
                    SupplierProduct.section_id == sec.section_id, SupplierProduct.heading == text))
                if prod is None:
                    prod = SupplierProduct(supplier_id=sup.supplier_id, section_id=sec.section_id, heading=text)
                    db.add(prod); await db.flush()
                label = f"{bill['bill_no']} · {qty} pcs"[:120]
                if await db.scalar(select(SupplierOffer).where(SupplierOffer.product_id == prod.product_id,
                                                               SupplierOffer.label == label)):
                    continue
                db.add(SupplierOffer(supplier_id=sup.supplier_id, product_id=prod.product_id, label=label,
                                     price=(Decimal(rate) * f).quantize(Decimal("0.01"), ROUND_HALF_UP),
                                     mrp=Decimal(mrp) if mrp else None, units_per_box=1))
                made += 1
            await db.flush()
        print("offers made", made)
        for sid in sups:
            print("suggest", await matching.suggest(db, sid))
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
