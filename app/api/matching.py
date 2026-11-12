"""Staff: keep the Online product mirror fresh and match supplier offers to it."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.api.schemas import ORM, Money
from app.core.db import get_db
from app.services import catalog, matching, online_catalog

router = APIRouter(tags=["matching"])


class OnlineProductOut(ORM):
    online_product_id: uuid.UUID
    sku: str | None
    name: str
    name_si: str | None
    display_size: str | None
    display_size_unit: str | None
    is_active: bool


class MatchProductOut(BaseModel):
    online_product_id: uuid.UUID
    online_variant_id: uuid.UUID | None
    name: str
    name_si: str | None
    sku: str | None
    size: str | None
    score: float | None


class QueueItemOut(BaseModel):
    offer_id: uuid.UUID
    heading: str
    label: str
    price: Money
    mrp: Money | None
    units_per_box: int | None
    confirmed: MatchProductOut | None
    suggestions: list[MatchProductOut]


class ConfirmIn(BaseModel):
    online_product_id: uuid.UUID
    online_variant_id: uuid.UUID | None = None


class RejectIn(BaseModel):
    online_product_id: uuid.UUID


@router.post("/online-catalog/sync")
async def sync_online(p: Principal = Depends(require("online.sync")),
                      db: AsyncSession = Depends(get_db)):
    return await online_catalog.sync(db, user_id=p.user_id)


@router.get("/online-products", response_model=list[OnlineProductOut])
async def search_online(q: str = Query(min_length=2, max_length=100),
                        _: Principal = Depends(require("matches.read")),
                        db: AsyncSession = Depends(get_db)):
    return await online_catalog.search(db, q)


@router.post("/suppliers/{supplier_id}/matching/suggest")
async def suggest(supplier_id: uuid.UUID, _: Principal = Depends(require("matches.write")),
                  db: AsyncSession = Depends(get_db)):
    await catalog.load(db, supplier_id)  # 404 for an unknown supplier
    return await matching.suggest(db, supplier_id)


@router.get("/suppliers/{supplier_id}/matching", response_model=list[QueueItemOut])
async def match_queue(supplier_id: uuid.UUID,
                      state: Literal["todo", "matched", "all"] = "todo",
                      _: Principal = Depends(require("matches.read")),
                      db: AsyncSession = Depends(get_db)):
    await catalog.load(db, supplier_id)
    return await matching.queue(db, supplier_id, state=state)


@router.post("/matching/offers/{offer_id}/confirm", status_code=204)
async def confirm(offer_id: uuid.UUID, body: ConfirmIn,
                  p: Principal = Depends(require("matches.write")),
                  db: AsyncSession = Depends(get_db)):
    await matching.confirm(db, offer_id, online_product_id=body.online_product_id,
                           online_variant_id=body.online_variant_id, user_id=p.user_id)


@router.post("/matching/offers/{offer_id}/reject", status_code=204)
async def reject(offer_id: uuid.UUID, body: RejectIn,
                 p: Principal = Depends(require("matches.write")),
                 db: AsyncSession = Depends(get_db)):
    await matching.reject(db, offer_id, online_product_id=body.online_product_id,
                          user_id=p.user_id)


@router.delete("/matching/offers/{offer_id}", status_code=204)
async def unmatch(offer_id: uuid.UUID, p: Principal = Depends(require("matches.write")),
                  db: AsyncSession = Depends(get_db)):
    await matching.unmatch(db, offer_id, user_id=p.user_id)
