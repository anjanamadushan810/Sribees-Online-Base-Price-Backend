"""Stock products, batches, settings (moved from Finance's purchasing routes, phase B) and
receiving goods against a supply order (replaces Finance's GRN)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.api.stock_schemas import (
    BatchOut,
    CloseRestIn,
    ProductIn,
    ProductOut,
    ProductPatch,
    ReceiptIn,
    ReceiptLineOut,
    ReceiptOut,
    SettingIn,
    SettingOut,
)
from app.core.db import get_db
from app.models import Label, Product, Receipt, Setting, StockBatch, SupplyOrder
from app.services import audit, receiving, settings, stock
from app.services.actors import Actor

router = APIRouter(tags=["stock"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


async def _names(db: AsyncSession, model, key, ids) -> dict:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {k: n for k, n in await db.execute(select(key, model.name).where(key.in_(ids)))}


# ---------------------------------------------------------------- settings
@router.get("/settings", response_model=list[SettingOut])
async def list_settings(
    _: Principal = Depends(require("stock.read")), db: AsyncSession = Depends(get_db)
):
    return list(await db.scalars(select(Setting).order_by(Setting.key)))


@router.patch("/settings/{key}", response_model=SettingOut)
async def update_setting(
    key: str,
    body: SettingIn,
    request: Request,
    p: Principal = Depends(require("settings.write")),
    db: AsyncSession = Depends(get_db),
):
    return await settings.set_value(db, key=key, value=body.value, user_id=p.user_id,
                                    ip_address=client_ip(request))


# ---------------------------------------------------------------- products
@router.get("/products", response_model=list[ProductOut])
async def list_products(
    search: str | None = Query(default=None, max_length=100),
    include_inactive: bool = False,
    kind: str | None = Query(default=None, pattern="^(sellable|bulk|material)$"),
    limit: int = Query(default=200, le=1000),
    _: Principal = Depends(require("products.read")),
    db: AsyncSession = Depends(get_db),
):
    q = select(Product).order_by(func.lower(Product.name)).limit(limit)
    if not include_inactive:
        q = q.where(Product.is_active)
    if kind:
        q = q.where(Product.kind == kind)
    if search:
        like = f"%{search}%"
        q = q.where(Product.name.ilike(like) | Product.sku.ilike(like))
    return list(await db.scalars(q))


@router.post("/products", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductIn,
    request: Request,
    p: Principal = Depends(require("products.write")),
    db: AsyncSession = Depends(get_db),
):
    data = body.model_dump()
    if body.kind == "sellable" and body.product_id is None:
        raise HTTPException(status_code=422, detail="an item we sell takes its Online product id")
    if body.kind == "bulk" and body.unit != "kg":
        raise HTTPException(status_code=422, detail="a bulk item is weighed in kg")
    if body.kind == "material" and body.unit != "each":
        raise HTTPException(status_code=422, detail="a packing material is counted in units")
    data["product_id"] = body.product_id or uuid.uuid4()
    if body.unit == "kg":
        data["weight_kg"] = Decimal("1")
    if await db.get(Product, data["product_id"]):
        raise HTTPException(status_code=409, detail="this product is already in the catalogue")
    product = Product(**data, stock_mode="loose" if body.unit == "kg" else "unit")
    db.add(product)
    await db.flush()
    await audit.record(db, user_id=p.user_id, action="product.create", entity="product",
                       entity_id=product.product_id, after=body.model_dump(mode="json"),
                       ip_address=client_ip(request))
    return product


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductPatch,
    request: Request,
    p: Principal = Depends(require("products.write")),
    db: AsyncSession = Depends(get_db),
):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    changes = body.model_dump(exclude_unset=True)
    if "unit" in changes and changes["unit"] != product.unit and await db.scalar(
        select(StockBatch.batch_id).where(StockBatch.product_id == product_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="the unit is fixed once stock was received")
    if "unit" in changes and product.stock_mode == "packed":
        raise HTTPException(status_code=409, detail="a pack size is counted in packs")
    if "unit" in changes and product.kind != "sellable":
        raise HTTPException(status_code=409, detail="a bulk item is weighed and a material counted")
    if "unit" in changes:
        product.stock_mode = "loose" if changes["unit"] == "kg" else "unit"
    if changes.get("weight_kg") is None:
        changes.pop("weight_kg", None)
    if (changes.get("unit") or product.unit) == "kg":
        changes["weight_kg"] = Decimal("1")
    before = ProductOut.model_validate(product).model_dump(mode="json")
    for field, value in changes.items():
        setattr(product, field, value)
    product.updated_at = datetime.now(UTC)
    await db.flush()
    await audit.record(db, user_id=p.user_id, action="product.update", entity="product",
                       entity_id=product_id, before=before,
                       after=ProductOut.model_validate(product).model_dump(mode="json"),
                       ip_address=client_ip(request))
    return product


# ---------------------------------------------------------------- stock
@router.get("/stock/batches", response_model=list[BatchOut])
async def list_batches(
    branch_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    supplier_id: uuid.UUID | None = None,
    expiring_within_days: int | None = Query(default=None, ge=0, le=365),
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    before = (date.today() + timedelta(days=expiring_within_days)
              if expiring_within_days is not None else None)
    batches = await stock.batches_on_hand(
        db, branch_ids=scoped_branches(p, branch_id), product_id=product_id,
        expiring_before=before,
    )
    if supplier_id is not None:
        batches = [b for b in batches if b.supplier_id == supplier_id]
    names = await _names(db, Product, Product.product_id, [b.product_id for b in batches])
    units = dict((await db.execute(select(Product.product_id, Product.unit).where(
        Product.product_id.in_({b.product_id for b in batches})))).all()) if batches else {}
    ids = [b.batch_id for b in batches]
    labelled = dict((await db.execute(
        select(Label.batch_id, func.count()).where(Label.batch_id.in_(ids))
        .group_by(Label.batch_id))).all()) if ids else {}
    return [BatchOut.model_validate(b).model_copy(update={
        "product_name": names[b.product_id], "labelled": labelled.get(b.batch_id, 0),
        "unit": units.get(b.product_id, "each")})
            for b in batches]


# ---------------------------------------------------------------- receiving
def _receipt_out(r: Receipt, names: dict) -> ReceiptOut:
    out = ReceiptOut.model_validate(r)
    return out.model_copy(update={"lines": [
        ReceiptLineOut.model_validate(ln).model_copy(
            update={"product_name": names.get(ln.product_id)})
        for ln in r.lines]})


@router.post("/orders/{order_id}/receipts", response_model=ReceiptOut, status_code=201)
async def receive_goods(
    order_id: uuid.UUID,
    body: ReceiptIn,
    request: Request,
    p: Principal = Depends(require("grn.create")),
    db: AsyncSession = Depends(get_db),
):
    receipt = await receiving.receive(
        db, _actor(p, request), order_id, received_date=body.received_date,
        invoice_no=body.invoice_no, invoice_total=body.invoice_total, notes=body.notes,
        transport_charge=body.transport_charge,
        lines=[receiving.ReceiptLineIn(**ln.model_dump()) for ln in body.lines],
        scope=scoped_branches(p, None))
    names = await _names(db, Product, Product.product_id, [ln.product_id for ln in receipt.lines])
    return _receipt_out(receipt, names)


@router.get("/orders/{order_id}/receipts", response_model=list[ReceiptOut])
async def order_receipts(
    order_id: uuid.UUID,
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    order = await db.get(SupplyOrder, order_id)
    if order is None or not p.can_see_branch(order.branch_id):
        raise HTTPException(status_code=404, detail="not found")
    rows = list(await db.scalars(select(Receipt).where(Receipt.order_id == order_id)
                                 .order_by(Receipt.created_at)))
    names = await _names(db, Product, Product.product_id,
                         [ln.product_id for r in rows for ln in r.lines])
    return [_receipt_out(r, names) for r in rows]


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut)
async def get_receipt(
    receipt_id: uuid.UUID,
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    receipt = await receiving.get_receipt(db, receipt_id, scoped_branches(p, None))
    names = await _names(db, Product, Product.product_id, [ln.product_id for ln in receipt.lines])
    return _receipt_out(receipt, names)


@router.post("/orders/{order_id}/close-rest", status_code=204)
async def close_rest(
    order_id: uuid.UUID,
    body: CloseRestIn,
    request: Request,
    p: Principal = Depends(require("grn.create")),
    db: AsyncSession = Depends(get_db),
):
    await receiving.close_rest(db, _actor(p, request), order_id, reason=body.reason,
                               scope=scoped_branches(p, None))


# ---------------------------------------------------------------- repack (B2)
class RepackOutIn(BaseModel):
    online_variant_id: uuid.UUID
    packs: int = Field(gt=0, le=100_000)
    labels: tuple[str, ...] = Field(default=(), max_length=5000)


class RepackIn(BaseModel):
    batch_id: uuid.UUID
    kg_used: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    repack_date: date
    notes: str | None = Field(default=None, max_length=1000)
    outputs: list[RepackOutIn] = Field(min_length=1, max_length=20)


@router.post("/stock/repacks", status_code=201)
async def repack_bag(body: RepackIn, request: Request,
                     p: Principal = Depends(require("stock.adjust")),
                     db: AsyncSession = Depends(get_db)):
    from app.services import repack

    return await repack.repack(
        db, _actor(p, request), batch_id=body.batch_id, kg_used=body.kg_used,
        repack_date=body.repack_date, notes=body.notes, scope=scoped_branches(p, None),
        outputs=[repack.RepackOut(**o.model_dump()) for o in body.outputs])


class CorrectionIn(BaseModel):
    batch_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    to_product_id: uuid.UUID  # the right Online pack size
    reason: str = Field(min_length=5, max_length=500)


@router.post("/stock/corrections", status_code=201)
async def correct_product(body: CorrectionIn, request: Request,
                          p: Principal = Depends(require("stock.adjust")),
                          db: AsyncSession = Depends(get_db)):
    """Owner 2026-11-26: stock received on the wrong product moves to the right one."""
    from app.services import correction

    return await correction.correct(db, _actor(p, request), batch_id=body.batch_id,
                                    qty=body.qty, to_product_id=body.to_product_id,
                                    reason=body.reason)
