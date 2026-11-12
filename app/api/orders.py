"""Supplier orders: staff side (/orders), supplier app (/supplier/orders), base prices."""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    SupplierPrincipal,
    current_principal,
    current_supplier,
    require,
)
from app.api.schemas import ORM, Money
from app.core.db import get_db
from app.models import Branch, OnlineProduct, Supplier, SupplyOrder
from app.services import orders
from app.services.errors import NotFound
from app.services.orders import StaffActor

router = APIRouter(tags=["orders"])


# ---------------------------------------------------------------- schemas
class LineOut(ORM):
    line_id: uuid.UUID
    offer_id: uuid.UUID
    heading: str
    label: str
    boxes: int
    units_per_box: int
    units: int
    unit_price: Money
    mrp: Money | None
    line_total: Money


class StaffLineOut(LineOut):
    online_product_id: uuid.UUID
    online_variant_id: uuid.UUID | None


class OrderBase(BaseModel):
    order_id: uuid.UUID
    order_no: str
    status: str
    branch_name: str
    goods_total: Money
    delivery_charge: Money | None
    total: Money
    supplier_note: str | None
    approved_at: datetime | None
    answered_at: datetime | None
    cancel_reason: str | None


class StaffOrderOut(OrderBase):
    supplier_id: uuid.UUID
    supplier_name: str
    branch_id: uuid.UUID
    note: str | None
    created_at: datetime
    submitted_at: datetime | None
    supplier_seen_at: datetime | None
    lines: list[StaffLineOut]


class SupplierOrderOut(OrderBase):
    lines: list[LineOut]


class OrderIn(BaseModel):
    supplier_id: uuid.UUID
    branch_id: uuid.UUID
    note: str | None = Field(default=None, max_length=2000)


class LineIn(BaseModel):
    offer_id: uuid.UUID
    boxes: int = Field(gt=0, le=100_000)


class BoxesIn(BaseModel):
    boxes: int = Field(ge=0, le=100_000)  # 0 removes the line


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AnswerIn(BaseModel):
    accept: bool
    delivery_charge: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    note: str | None = Field(default=None, max_length=1000)


class BasePriceOut(ORM):
    online_product_id: uuid.UUID
    online_variant_id: uuid.UUID | None
    supplier_id: uuid.UUID
    order_id: uuid.UUID
    unit_price: Money
    mrp: Money | None
    recorded_at: datetime
    voided_at: datetime | None


class BasePriceRowOut(BasePriceOut):
    product_name: str
    supplier_name: str


class OfferCompareOut(BaseModel):
    offer_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_name: str
    heading: str
    label: str
    price: Money
    mrp: Money | None
    units_per_box: int | None
    online_variant_id: uuid.UUID | None


class ProductBasePriceOut(BaseModel):
    online_product_id: uuid.UUID
    name: str
    history: list[BasePriceOut]
    offers: list[OfferCompareOut]


# ---------------------------------------------------------------- helpers
def _actor(p: Principal) -> StaffActor:
    return StaffActor(p.user_id, p.role, p.branch_scope())


async def _names(db, order: SupplyOrder) -> tuple[str, str]:
    supplier = await db.get(Supplier, order.supplier_id)
    branch = await db.get(Branch, order.branch_id)
    return supplier.name, branch.name


async def _staff_out(db, order: SupplyOrder) -> StaffOrderOut:
    supplier_name, branch_name = await _names(db, order)
    return StaffOrderOut(
        **{k: getattr(order, k) for k in (
            "order_id", "order_no", "status", "supplier_note", "approved_at", "answered_at",
            "cancel_reason", "supplier_id", "branch_id", "note", "created_at",
            "submitted_at", "supplier_seen_at")},
        **orders.order_totals(order), supplier_name=supplier_name, branch_name=branch_name,
        lines=[StaffLineOut.model_validate(ln) for ln in order.lines])


async def _supplier_out(db, order: SupplyOrder) -> SupplierOrderOut:
    _, branch_name = await _names(db, order)
    return SupplierOrderOut(
        **{k: getattr(order, k) for k in (
            "order_id", "order_no", "status", "supplier_note", "approved_at", "answered_at",
            "cancel_reason")},
        **orders.order_totals(order), branch_name=branch_name,
        lines=[LineOut.model_validate(ln) for ln in order.lines])


# ---------------------------------------------------------------- staff
@router.get("/orders", response_model=list[StaffOrderOut])
async def list_orders(supplier_id: uuid.UUID | None = None, status: str | None = None,
                      p: Principal = Depends(require("orders.read")),
                      db: AsyncSession = Depends(get_db)):
    return [await _staff_out(db, o) for o in
            await orders.list_orders(db, _actor(p), supplier_id=supplier_id, status=status)]


@router.post("/orders", response_model=StaffOrderOut, status_code=201)
async def create_order(body: OrderIn, p: Principal = Depends(current_principal),
                       db: AsyncSession = Depends(get_db)):
    order = await orders.create(db, _actor(p), supplier_id=body.supplier_id,
                                branch_id=body.branch_id, note=body.note)
    return await _staff_out(db, order)


@router.get("/orders/{order_id}", response_model=StaffOrderOut)
async def get_order(order_id: uuid.UUID, p: Principal = Depends(require("orders.read")),
                    db: AsyncSession = Depends(get_db)):
    return await _staff_out(db, await orders.get(db, order_id, _actor(p)))


@router.post("/orders/{order_id}/lines", response_model=StaffOrderOut)
async def add_line(order_id: uuid.UUID, body: LineIn, p: Principal = Depends(current_principal),
                   db: AsyncSession = Depends(get_db)):
    order = await orders.add_line(db, _actor(p), order_id, offer_id=body.offer_id,
                                  boxes=body.boxes)
    return await _staff_out(db, order)


@router.patch("/orders/{order_id}/lines/{line_id}", response_model=StaffOrderOut)
async def set_boxes(order_id: uuid.UUID, line_id: uuid.UUID, body: BoxesIn,
                    p: Principal = Depends(current_principal),
                    db: AsyncSession = Depends(get_db)):
    order = await orders.set_boxes(db, _actor(p), order_id, line_id, boxes=body.boxes)
    return await _staff_out(db, order)


@router.post("/orders/{order_id}/submit", response_model=StaffOrderOut)
async def submit(order_id: uuid.UUID, p: Principal = Depends(current_principal),
                 db: AsyncSession = Depends(get_db)):
    return await _staff_out(db, await orders.submit(db, _actor(p), order_id))


@router.post("/orders/{order_id}/approve", response_model=StaffOrderOut)
async def approve(order_id: uuid.UUID, p: Principal = Depends(current_principal),
                  db: AsyncSession = Depends(get_db)):
    return await _staff_out(db, await orders.approve(db, _actor(p), order_id))


@router.post("/orders/{order_id}/cancel", response_model=StaffOrderOut)
async def cancel(order_id: uuid.UUID, body: CancelIn, p: Principal = Depends(current_principal),
                 db: AsyncSession = Depends(get_db)):
    return await _staff_out(db, await orders.cancel(db, _actor(p), order_id, reason=body.reason))


# ---------------------------------------------------------------- base prices
@router.get("/base-prices", response_model=list[BasePriceRowOut])
async def base_prices(_: Principal = Depends(require("base_prices.read")),
                      db: AsyncSession = Depends(get_db)):
    rows = await orders.current_base_prices(db)
    names: dict = {}
    if rows:
        ids = [r.online_product_id for r in rows]
        names = dict((await db.execute(
            select(OnlineProduct.online_product_id, OnlineProduct.name)
            .where(OnlineProduct.online_product_id.in_(ids)))).all())
    suppliers = dict((await db.execute(select(Supplier.supplier_id, Supplier.name))).all())
    return [BasePriceRowOut(**BasePriceOut.model_validate(r).model_dump(),
                            product_name=names.get(r.online_product_id, ""),
                            supplier_name=suppliers.get(r.supplier_id, "")) for r in rows]


@router.get("/base-prices/{online_product_id}", response_model=ProductBasePriceOut)
async def product_base_price(online_product_id: uuid.UUID,
                             _: Principal = Depends(require("base_prices.read")),
                             db: AsyncSession = Depends(get_db)):
    product = await db.get(OnlineProduct, online_product_id)
    if product is None:
        raise NotFound("Online product not found")
    return ProductBasePriceOut(
        online_product_id=online_product_id, name=product.name,
        history=await orders.base_price_history(db, online_product_id),
        offers=await orders.offers_for_product(db, online_product_id))


# ---------------------------------------------------------------- supplier app
@router.get("/supplier/orders", response_model=list[SupplierOrderOut])
async def my_orders(status: str | None = Query(default=None),
                    p: SupplierPrincipal = Depends(current_supplier),
                    db: AsyncSession = Depends(get_db)):
    rows = await orders.supplier_orders(db, p.supplier_id)
    return [await _supplier_out(db, o) for o in rows if status is None or o.status == status]


@router.get("/supplier/orders/{order_id}", response_model=SupplierOrderOut)
async def my_order(order_id: uuid.UUID, p: SupplierPrincipal = Depends(current_supplier),
                   db: AsyncSession = Depends(get_db)):
    return await _supplier_out(db, await orders.supplier_order(db, p.supplier_id, order_id))


@router.post("/supplier/orders/{order_id}/answer", response_model=SupplierOrderOut)
async def answer(order_id: uuid.UUID, body: AnswerIn,
                 p: SupplierPrincipal = Depends(current_supplier),
                 db: AsyncSession = Depends(get_db)):
    order = await orders.answer(db, p.supplier_id, p.user.supplier_user_id, order_id,
                                accept=body.accept, delivery_charge=body.delivery_charge,
                                note=body.note)
    return await _supplier_out(db, order)
