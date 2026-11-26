"""Quick buy: a market cash buy ordered and received in one step (app/services/quick_buy.py)."""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require
from app.api.stock_schemas import PriceIn, ReceiptOut
from app.core.db import get_db
from app.models import Product
from app.services import quick_buy
from app.services.actors import Actor
from app.services.orders import StaffActor

router = APIRouter(tags=["quick buy"])


class QuickLineIn(BaseModel):
    online_variant_id: uuid.UUID
    units: int = Field(gt=0, le=1_000_000)
    unit_price: PriceIn
    free_units: int = Field(default=0, ge=0, le=1_000_000)
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    labels: tuple[str, ...] = Field(default=(), max_length=5000)


class QuickBuyIn(BaseModel):
    supplier_id: uuid.UUID
    branch_id: uuid.UUID
    received_date: date
    bill_no: str | None = Field(default=None, max_length=60)
    bill_total: PriceIn | None = None
    notes: str | None = Field(default=None, max_length=1000)
    lines: list[QuickLineIn] = Field(min_length=1, max_length=quick_buy.MAX_LINES)


class QuickBuyOut(BaseModel):
    order_id: uuid.UUID
    order_no: str
    order_status: str
    receipt: ReceiptOut


class ItemOut(BaseModel):
    online_variant_id: uuid.UUID
    online_product_id: uuid.UUID
    name: str
    name_si: str | None
    variant: str
    weight_grams: int | None


@router.get("/quick-buys/items", response_model=list[ItemOut])
async def items(q: str = Query(min_length=2, max_length=100),
                _: Principal = Depends(require("grn.create")),
                db: AsyncSession = Depends(get_db)):
    return await quick_buy.search_items(db, q)


@router.post("/quick-buys", response_model=QuickBuyOut, status_code=201)
async def create(body: QuickBuyIn, request: Request,
                 p: Principal = Depends(require("grn.create")),
                 db: AsyncSession = Depends(get_db)):
    from app.api.stock_products import _names, _receipt_out

    order, receipt = await quick_buy.quick_buy(
        db, StaffActor(p.user_id, p.role, p.branch_scope()),
        Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request)),
        supplier_id=body.supplier_id, branch_id=body.branch_id,
        received_date=body.received_date, bill_no=body.bill_no, bill_total=body.bill_total,
        notes=body.notes,
        lines=[quick_buy.QuickLine(**ln.model_dump()) for ln in body.lines])
    names = await _names(db, Product, Product.product_id, [ln.product_id for ln in receipt.lines])
    return QuickBuyOut(order_id=order.order_id, order_no=order.order_no,
                       order_status=order.status, receipt=_receipt_out(receipt, names))
