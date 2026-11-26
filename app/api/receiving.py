"""The receiving page (owner 2026-11-26): what is due, what the transport split would
give, what the shop sells each item for now, and whether Online holds the stock yet."""

import uuid
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.api.stock_schemas import Money, PriceIn
from app.core.config import get_settings
from app.core.db import get_db
from app.models import OnlineStockSend, Product, Receipt, ReceiptLine
from app.services import online_stock, receiving
from app.services.actors import Actor
from app.services.errors import NotFound, SupplyError

router = APIRouter(tags=["receiving"])


class PreviewLineIn(BaseModel):
    order_line_id: uuid.UUID
    units: int = Field(gt=0, le=1_000_000)
    free_units: int = Field(default=0, ge=0, le=1_000_000)
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    product_id: uuid.UUID | None = None
    unit_price: PriceIn | None = None


class PreviewIn(BaseModel):
    transport_charge: PriceIn | None = None
    lines: list[PreviewLineIn] = Field(max_length=500)


class PreviewLineOut(BaseModel):
    order_line_id: uuid.UUID
    name: str
    units: int
    unit_price: Money
    value: Money
    weight_grams: int | None
    transport: Money | None
    delivered_unit_price: Money | None


class PreviewOut(BaseModel):
    transport_charge: Money
    goods_total: Money
    total: Money
    transport_cap: Money  # above this only the owner saves
    over_cap: bool
    missing_weight: list[str]
    lines: list[PreviewLineOut]


class PackSizeOut(BaseModel):
    online_variant_id: uuid.UUID
    name: str


class ShopOut(BaseModel):
    """What Online holds and sells the item for at this branch now."""

    stock_mode: str | None = None
    stock_quantity: int | None = None
    stock_grams: int | None = None
    has_batches: bool | None = None
    listed_price: str | None = None
    selling_price: str | None = None
    visible: bool | None = None


class FormLineOut(BaseModel):
    order_line_id: uuid.UUID
    name: str
    name_si: str | None
    image_url: str | None
    online_variant_id: uuid.UUID | None
    pack_sizes: list[PackSizeOut]
    due_units: int
    unit_price: Money
    mrp: Money | None
    by_weight: bool
    unit_weight_grams: int | None
    shop: ShopOut | None = None


class FormOut(BaseModel):
    order_id: uuid.UUID
    order_no: str
    status: str
    supplier_name: str
    branch_name: str
    transport_charge: Money  # the supplier's figure, offered to staff
    transport_cap_pct: int
    labels_required: bool
    online_stock_in: bool
    online_error: str | None = None
    lines: list[FormLineOut]


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


@router.get("/orders/{order_id}/receiving", response_model=FormOut)
async def receiving_form(order_id: uuid.UUID, request: Request,
                         p: Principal = Depends(require("grn.create")),
                         db: AsyncSession = Depends(get_db)):
    """The lines still due. Online's current stock and prices are added when it answers;
    receiving works without them (the shop's prices are then left as they are)."""
    out = await receiving.form(db, _actor(p, request), order_id, scoped_branches(p, None))
    out["online_stock_in"] = online_stock.enabled()
    out["online_error"] = None
    out["transport_cap_pct"] = get_settings().delivery_charge_max_pct
    wanted = [ln for ln in out["lines"] if ln["online_variant_id"] and ln["online_product_id"]]
    # prices are shown (read only on Online) even before stock sending is switched on
    if get_settings().online_api_url and out["online_branch_id"] and wanted:
        try:
            async with httpx.AsyncClient() as client:
                info = await online_stock.stock_info(
                    client, out["online_branch_id"],
                    [(ln["online_product_id"], ln["online_variant_id"]) for ln in wanted[:500]])
            for ln, item in zip(wanted, info.get("items") or [], strict=False):
                if item.get("known"):
                    ln["shop"] = {k: item.get(k) for k in (
                        "stock_mode", "stock_quantity", "stock_grams", "has_batches",
                        "listed_price", "selling_price", "visible")}
        except SupplyError as exc:
            out["online_error"] = str(exc)
    return out


@router.post("/orders/{order_id}/receipts/preview", response_model=PreviewOut)
async def preview(order_id: uuid.UUID, body: PreviewIn, request: Request,
                  p: Principal = Depends(require("grn.create")),
                  db: AsyncSession = Depends(get_db)):
    return await receiving.preview(
        db, _actor(p, request), order_id, transport_charge=body.transport_charge,
        lines=[receiving.ReceiptLineIn(**ln.model_dump()) for ln in body.lines],
        scope=scoped_branches(p, None))


@router.get("/receipts/{receipt_id}/online-stock")
async def online_state(receipt_id: uuid.UUID, p: Principal = Depends(require("stock.read")),
                       db: AsyncSession = Depends(get_db)):
    """Per received line: pending, sent or refused (with Online's reason). Empty = this
    receipt is not sent (the switch was off when it was saved)."""
    receipt = await db.get(Receipt, receipt_id)
    if receipt is None or not p.can_see_branch(receipt.branch_id):
        raise NotFound("receipt not found")
    rows = await db.execute(
        select(OnlineStockSend, Product.name, ReceiptLine.line_no)
        .join(ReceiptLine, ReceiptLine.receipt_line_id == OnlineStockSend.receipt_line_id)
        .join(Product, Product.product_id == ReceiptLine.product_id)
        .where(OnlineStockSend.receipt_id == receipt_id).order_by(ReceiptLine.line_no))
    return [{"receipt_line_id": s.receipt_line_id, "line_no": n, "product_name": name,
             "status": s.status, "reason": s.reason, "attempts": s.attempts}
            for s, name, n in rows]


@router.post("/receipts/{receipt_id}/online-stock/retry")
async def retry(receipt_id: uuid.UUID, p: Principal = Depends(require("grn.create")),
                db: AsyncSession = Depends(get_db)):
    return {"queued": await online_stock.retry(db, receipt_id, user_id=p.user_id,
                                               scope=scoped_branches(p, None))}
