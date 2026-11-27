"""Fulfilment partner (app/services/partners.py, partner_requests.py): staff set-up, the
partner's own item list (price to us + stock) and the customer orders it packs."""

import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    Principal,
    SupplierPrincipal,
    client_ip,
    current_supplier,
    require,
    scoped_branches,
)
from app.core.db import get_db
from app.models import Branch
from app.services import partner_requests, partner_sync, partners
from app.services.actors import Actor

router = APIRouter(tags=["fulfilment partner"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


class KindIn(BaseModel):
    kind: Literal["wholesale", "fulfilment_partner"]


class BranchIn(BaseModel):
    branch_id: uuid.UUID
    # F2: also fill the part of an Our Store line the shelf cannot cover (off until the
    # branch's stock in Supply is complete)
    fills_store_shortfall: bool = False


class CustomerPriceIn(BaseModel):
    branch_id: uuid.UUID
    online_variant_id: uuid.UUID
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


async def _with_customer_prices(db: AsyncSession, rows: list[dict], *, price_key: str) -> None:
    """Add Online's current customer price to each row (and `loss` when it is below the
    partner's price to us). Best effort: without the link the rows stay as they are."""
    if not rows or not partner_sync.enabled():
        return
    by_branch: dict = {}
    for r in rows:
        by_branch.setdefault(r["branch_id"], []).append(r)
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient() as client:
        for bid, group in by_branch.items():
            branch = await db.get(Branch, uuid.UUID(bid))
            if branch is None or branch.online_branch_id is None:
                continue
            refs = [(uuid.UUID(r["online_product_id"]), uuid.UUID(r["online_variant_id"]))
                    for r in group]
            prices = await partner_sync.current_prices(client, branch.online_branch_id, refs)
            for r, (_p, v) in zip(group, refs, strict=True):
                r["customer_price"] = prices.get(v)
                to_us = r.get(price_key)
                r["loss"] = bool(r["customer_price"] and to_us
                                 and Decimal(r["customer_price"]) < Decimal(to_us))


class ItemIn(BaseModel):
    branch_id: uuid.UUID
    online_variant_id: uuid.UUID
    price_to_us: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    stock_qty: int | None = Field(default=None, ge=0, le=1_000_000)


# ---------------------------------------------------------------- staff
@router.put("/partners/{supplier_id}/kind")
async def set_kind(supplier_id: uuid.UUID, body: KindIn, request: Request,
                   p: Principal = Depends(require("partners.write")),
                   db: AsyncSession = Depends(get_db)):
    return await partners.set_kind(db, _actor(p, request), supplier_id, body.kind)


@router.post("/partners/{supplier_id}/branches", status_code=201)
async def link_branch(supplier_id: uuid.UUID, body: BranchIn, request: Request,
                      p: Principal = Depends(require("partners.write")),
                      db: AsyncSession = Depends(get_db)):
    return await partners.link_branch(db, _actor(p, request), supplier_id, body.branch_id,
                                      p.branch_scope(),
                                      fills_store_shortfall=body.fills_store_shortfall)


@router.delete("/partners/{supplier_id}/branches/{branch_id}")
async def unlink_branch(supplier_id: uuid.UUID, branch_id: uuid.UUID, request: Request,
                        p: Principal = Depends(require("partners.write")),
                        db: AsyncSession = Depends(get_db)):
    return await partners.unlink_branch(db, _actor(p, request), supplier_id, branch_id,
                                        p.branch_scope())


@router.get("/partners/sourcing")
async def sourcing(branch_id: uuid.UUID, mode: Literal["own", "partner"] | None = None,
                   q: str | None = Query(default=None, max_length=100),
                   limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0),
                   p: Principal = Depends(require("partners.read")),
                   db: AsyncSession = Depends(get_db)):
    return await partners.sourcing_list(db, branch_id, p.branch_scope(), mode=mode, q=q,
                                        limit=limit, offset=offset)


@router.get("/partners/share")
async def share(branch_id: uuid.UUID, p: Principal = Depends(require("partners.read")),
                db: AsyncSession = Depends(get_db)):
    return await partners.share(db, branch_id, p.branch_scope())


@router.get("/partners/price-changes")
async def price_changes(branch_id: uuid.UUID | None = None, all: bool = False,
                        p: Principal = Depends(require("partners.read")),
                        db: AsyncSession = Depends(get_db)):
    scope = scoped_branches(p, None)
    rows = await partners.price_changes(db, scope, branch_id=branch_id, unseen_only=not all)
    await _with_customer_prices(db, rows, price_key="price_to_us_now")
    return rows


@router.get("/partners/requests")
async def staff_requests(branch_id: uuid.UUID | None = None,
                         status: Literal["open", "new", "seen", "ready", "handed",
                                         "cancelled"] | None = "open",
                         limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0),
                         p: Principal = Depends(require("partners.read")),
                         db: AsyncSession = Depends(get_db)):
    return await partner_requests.staff_list(db, p.branch_scope(), branch_id=branch_id,
                                             status=status, limit=limit, offset=offset)


@router.get("/partners/requests/{request_id}")
async def staff_request(request_id: uuid.UUID,
                        p: Principal = Depends(require("partners.read")),
                        db: AsyncSession = Depends(get_db)):
    fr = await partner_requests.staff_get(db, p.branch_scope(), request_id=request_id)
    return partner_requests.summary(fr, with_customer=True)


@router.post("/partners/price-changes/{change_id}/seen", status_code=204)
async def mark_seen(change_id: int, request: Request,
                    p: Principal = Depends(require("partners.read")),
                    db: AsyncSession = Depends(get_db)):
    await partners.mark_seen(db, _actor(p, request), change_id, p.branch_scope())


# ---------------------------------------------------------------- partner app
@router.get("/supplier/partner/items")
async def my_items(q: str | None = Query(default=None, max_length=100),
                   branch_id: uuid.UUID | None = None,
                   limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0),
                   sp: SupplierPrincipal = Depends(current_supplier),
                   db: AsyncSession = Depends(get_db)):
    out = await partners.my_items(db, sp.supplier_id, q=q, branch_id=branch_id,
                                  limit=limit, offset=offset)
    await _with_customer_prices(db, out["items"], price_key="price_to_us")
    return out


@router.put("/supplier/partner/items/customer-price")
async def set_customer_price(body: CustomerPriceIn, request: Request,
                             sp: SupplierPrincipal = Depends(current_supplier),
                             db: AsyncSession = Depends(get_db)):
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient() as client:
        return await partners.set_customer_price(
            db, client, sp.supplier_id, sp.user.supplier_user_id, branch_id=body.branch_id,
            online_variant_id=body.online_variant_id, price=body.price,
            ip_address=client_ip(request))


@router.put("/supplier/partner/items")
async def update_item(body: ItemIn, request: Request,
                      sp: SupplierPrincipal = Depends(current_supplier),
                      db: AsyncSession = Depends(get_db)):
    sent = body.model_fields_set
    return await partners.update_item(
        db, sp.supplier_id, sp.user.supplier_user_id, branch_id=body.branch_id,
        online_variant_id=body.online_variant_id,
        price_to_us=body.price_to_us if "price_to_us" in sent else partners.UNSET,
        stock_qty=body.stock_qty if "stock_qty" in sent else partners.UNSET,
        ip_address=client_ip(request))


@router.get("/supplier/partner/requests")
async def my_requests(open_only: bool = True,
                      limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0),
                      sp: SupplierPrincipal = Depends(current_supplier),
                      db: AsyncSession = Depends(get_db)):
    return await partner_requests.partner_list(db, sp.supplier_id, open_only=open_only,
                                               limit=limit, offset=offset)


@router.get("/supplier/partner/requests/{request_id}")
async def my_request(request_id: uuid.UUID,
                     sp: SupplierPrincipal = Depends(current_supplier),
                     db: AsyncSession = Depends(get_db)):
    return await partner_requests.partner_open(db, sp.supplier_id, request_id)


@router.post("/supplier/partner/requests/{request_id}/ready")
async def request_ready(request_id: uuid.UUID,
                        sp: SupplierPrincipal = Depends(current_supplier),
                        db: AsyncSession = Depends(get_db)):
    return await partner_requests.partner_ready(db, sp.supplier_id, request_id)
