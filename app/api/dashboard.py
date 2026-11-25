"""Home page numbers. Read-only. Orders are limited to the person's branches."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.core.db import get_db
from app.services import dashboard
from app.services.orders import StaffActor

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _actor(p: Principal) -> StaffActor:
    return StaffActor(p.user_id, p.role, p.branch_scope())


@router.get("/today")
async def today(p: Principal = Depends(require("suppliers.read")),
                db: AsyncSession = Depends(get_db)):
    return await dashboard.today(db, _actor(p))


@router.get("/orders-summary")
async def orders_summary(p: Principal = Depends(require("orders.read")),
                         db: AsyncSession = Depends(get_db)):
    return await dashboard.orders_summary(db, _actor(p))


@router.get("/price-changes")
async def price_changes(days: int = Query(7, ge=1, le=90),
                        _: Principal = Depends(require("suppliers.read")),
                        db: AsyncSession = Depends(get_db)):
    return await dashboard.price_changes(db, days)


@router.get("/base-price-insight")
async def base_price_insight(_: Principal = Depends(require("base_prices.read")),
                             db: AsyncSession = Depends(get_db)):
    return await dashboard.base_price_insight(db)
