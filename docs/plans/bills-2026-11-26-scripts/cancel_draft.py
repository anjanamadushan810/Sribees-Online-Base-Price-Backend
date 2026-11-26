import asyncio, sys, uuid
from sqlalchemy import select
from app.core.db import get_sessionmaker
from app.models import SupplyOrder
from app.services import orders
OWNER = uuid.UUID("21e57b57-20a2-4dc7-bdbf-cb99e4a1988f")
async def main():
    async with get_sessionmaker()() as db, db.begin():
        o = await db.scalar(select(SupplyOrder).where(SupplyOrder.order_no == "SPO-000001"))
        assert o.status == "draft"
        await orders.cancel(db, orders.StaffActor(OWNER, "owner", None), o.order_id,
                            reason="Empty draft, owner 2026-11-26")
        print(o.order_no, o.status)
asyncio.run(main())
