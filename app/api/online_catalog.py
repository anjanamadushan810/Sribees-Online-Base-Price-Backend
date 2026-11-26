"""Staff: keep the Online product mirror fresh and find our products (with photos).

Owner 2026-11-26: orders name OUR products; supplier names are not matched any more."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.api.schemas import ORM
from app.core.db import get_db
from app.services import online_catalog, quick_buy

router = APIRouter(tags=["online catalog"])


class OnlineProductOut(ORM):
    online_product_id: uuid.UUID
    sku: str | None
    name: str
    name_si: str | None
    display_size: str | None
    display_size_unit: str | None
    is_active: bool
    image_url: str | None = None


@router.post("/online-catalog/sync")
async def sync_online(p: Principal = Depends(require("online.sync")),
                      db: AsyncSession = Depends(get_db)):
    return await online_catalog.sync(db, user_id=p.user_id)


@router.get("/online-products", response_model=list[OnlineProductOut])
async def search_online(q: str = Query(min_length=2, max_length=100),
                        _: Principal = Depends(require("orders.read")),
                        db: AsyncSession = Depends(get_db)):
    return await online_catalog.search(db, q)


@router.get("/online-items")
async def search_items(q: str = Query(min_length=2, max_length=100),
                       _: Principal = Depends(require("orders.read")),
                       db: AsyncSession = Depends(get_db)):
    """Our pack sizes by name (English or Sinhala) or SKU, with photo and the last price we
    paid: what an order line names."""
    return await quick_buy.search_items(db, q)
