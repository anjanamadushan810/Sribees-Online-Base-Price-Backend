"""Staff app: receive a delivery product by product (plan 2026-11-27, P2)."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.api.stock_products import _names, _receipt_out
from app.api.stock_schemas import PriceIn, ReceiptOut
from app.core.db import get_db
from app.models import Product
from app.services import drafts
from app.services.actors import Actor

router = APIRouter(tags=["receipt drafts"])
GRN = require("grn.create")


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


class LabelIn(BaseModel):
    number: str = Field(min_length=10, max_length=20)


class LineIn(BaseModel):
    order_line_id: uuid.UUID | None = None
    online_variant_id: uuid.UUID | None = None
    barcode: str | None = Field(default=None, max_length=20)
    units: int = Field(gt=0, le=1_000_000)
    free_units: int = Field(default=0, ge=0, le=1_000_000)
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    expiry_date: date
    mfg_date: date | None = None
    mrp: PriceIn | None = None
    unit_price: PriceIn | None = None
    listed_price: PriceIn | None = None
    selling_price: PriceIn | None = None
    labels: list[str] = Field(default=[], max_length=5000)


class FinishIn(BaseModel):
    received_date: date
    invoice_no: str | None = Field(default=None, max_length=60)
    invoice_total: PriceIn | None = None
    notes: str | None = Field(default=None, max_length=2000)
    transport_charge: PriceIn | None = None


async def _draft(db, p, draft_id, lock=True):
    return await drafts.get_draft(db, draft_id, scoped_branches(p, None), lock=lock)


@router.post("/orders/{order_id}/receipt-draft")
async def open_draft(order_id: uuid.UUID, request: Request, p: Principal = Depends(GRN),
                     db: AsyncSession = Depends(get_db)):
    scope = scoped_branches(p, None)
    draft = await drafts.open_draft(db, _actor(p, request), order_id, scope)
    d, order = await drafts.get_draft(db, draft.draft_id, scope)
    return await drafts.view(db, d, order)


@router.get("/receipt-drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID, p: Principal = Depends(GRN),
                    db: AsyncSession = Depends(get_db)):
    d, order = await drafts.get_draft(db, draft_id, scoped_branches(p, None), open_only=False)
    return await drafts.view(db, d, order)


@router.post("/receipt-drafts/{draft_id}/labels")
async def reserve_label(draft_id: uuid.UUID, body: LabelIn, request: Request,
                        p: Principal = Depends(GRN), db: AsyncSession = Depends(get_db)):
    d, _ = await _draft(db, p, draft_id)
    return {"number": await drafts.reserve(db, _actor(p, request), d, body.number)}


@router.delete("/receipt-drafts/{draft_id}/labels/{number}", status_code=204)
async def release_label(draft_id: uuid.UUID, number: str, p: Principal = Depends(GRN),
                        db: AsyncSession = Depends(get_db)):
    d, _ = await _draft(db, p, draft_id)
    await drafts.release(db, d, number)
    return Response(status_code=204)


@router.post("/receipt-drafts/{draft_id}/lines", status_code=201)
async def add_line(draft_id: uuid.UUID, body: LineIn, request: Request,
                   p: Principal = Depends(GRN), db: AsyncSession = Depends(get_db)):
    d, order = await _draft(db, p, draft_id)
    data = body.model_dump()
    data["labels"] = tuple(data["labels"])
    await drafts.add_line(db, _actor(p, request), d, order, drafts.DraftLineIn(**data))
    return await drafts.view(db, d, order)


@router.delete("/receipt-drafts/{draft_id}/lines/{line_id}")
async def remove_line(draft_id: uuid.UUID, line_id: uuid.UUID, p: Principal = Depends(GRN),
                      db: AsyncSession = Depends(get_db)):
    d, order = await _draft(db, p, draft_id)
    await drafts.remove_line(db, d, line_id)
    return await drafts.view(db, d, order)


@router.post("/receipt-drafts/{draft_id}/finish", response_model=ReceiptOut, status_code=201)
async def finish(draft_id: uuid.UUID, body: FinishIn, request: Request,
                 p: Principal = Depends(GRN), db: AsyncSession = Depends(get_db)):
    scope = scoped_branches(p, None)
    d, _ = await _draft(db, p, draft_id)
    receipt = await drafts.finish(db, _actor(p, request), d, scope=scope, **body.model_dump())
    names = await _names(db, Product, Product.product_id, [ln.product_id for ln in receipt.lines])
    return _receipt_out(receipt, names)


@router.delete("/receipt-drafts/{draft_id}", status_code=204)
async def discard(draft_id: uuid.UUID, request: Request, p: Principal = Depends(GRN),
                  db: AsyncSession = Depends(get_db)):
    d, _ = await _draft(db, p, draft_id)
    await drafts.discard(db, _actor(p, request), d)
    return Response(status_code=204)
