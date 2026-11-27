"""Branch manager's dashboard: new shop prices of delivered batches (plan 2026-11-27, P6)."""

import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.api.stock_schemas import PriceIn
from app.core.db import get_db
from app.services import online_stock, price_reviews
from app.services.actors import Actor

router = APIRouter(tags=["price reviews"])


class ReviewIn(BaseModel):
    selling_price: PriceIn = Field(gt=0)


@router.get("/price-reviews")
async def waiting(p: Principal = Depends(require("grn.create")),
                  db: AsyncSession = Depends(get_db)):
    return await price_reviews.waiting(db, scoped_branches(p, None))


@router.post("/price-reviews/{receipt_line_id}")
async def submit(receipt_line_id: uuid.UUID, body: ReviewIn, request: Request,
                 p: Principal = Depends(require("grn.create")),
                 db: AsyncSession = Depends(get_db)):
    actor = Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))
    async with httpx.AsyncClient() as client:
        return await price_reviews.submit(
            db, actor, price_reviews.Decision(receipt_line_id, body.selling_price),
            scope=scoped_branches(p, None), can_sell_below_cost=p.role == "owner",
            client=client, post=online_stock._post)
