"""Owner 2026-11-26: my 'Pond's Pure Detox Charcoal Face Wash' duplicates the owner's
'Pond's Bright Miracle Facewash 50ml'. Move its 2 received units (and every record) over."""
import asyncio
import sys
import uuid

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.models import OnlineProduct, Product, SupplierProduct
from app.services import audit
from app.services.receiving import ensure_product

COMMIT = "commit" in sys.argv
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
X = uuid.UUID("6de9fcb5-0b24-4dcd-8f9d-9720dc08c537")  # duplicate
Y = uuid.UUID("982d00e0-0bfc-48b4-9a59-a05e6addc962")  # Pond's Bright Miracle Facewash 50ml


async def main():
    async with get_sessionmaker()() as db:
        tx = await db.begin()
        assert await db.get(Product, Y) is None
        y = await ensure_product(db, Y)
        x = await db.get(Product, X)
        y.unit, y.stock_mode, y.kind, y.weight_kg = x.unit, x.stock_mode, x.kind, x.weight_kg
        await db.flush()
        n = {}
        for table, cols in [("stock_batches", ["product_id"]), ("receipt_lines", ["product_id"]),
                            ("offer_matches", ["online_product_id", "online_variant_id"]),
                            ("supply_order_lines", ["online_product_id", "online_variant_id"]),
                            ("base_prices", ["online_product_id", "online_variant_id"])]:
            for c in cols:
                r = await db.execute(text(f"UPDATE {table} SET {c} = :y WHERE {c} = :x"),
                                     {"x": X, "y": Y})
                n[f"{table}.{c}"] = r.rowcount
        yname = (await db.get(OnlineProduct, Y)).name
        r = await db.execute(text("UPDATE supplier_products SET heading = :h "
                                  "WHERE heading = :old"),
                             {"h": yname, "old": "Pond's Pure Detox Charcoal Face Wash"})
        n["supplier_products.heading"] = r.rowcount
        await db.delete(x)
        await db.flush()
        await audit.record(db, user_id=OWNER, action="product.merge_duplicate", entity="product",
                           entity_id=Y, after={"from": str(X), "to": str(Y), "rows": n,
                                               "why": "duplicate made from a bill; owner 2026-11-26"})
        print(n, "->", yname)
        if COMMIT:
            await tx.commit(); print("COMMITTED")
        else:
            await tx.rollback(); print("dry run")

asyncio.run(main())
