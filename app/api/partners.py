"""Fulfilment partner, phase P1 (app/services/partners.py): staff set-up and the partner's
own item list (price to us + stock)."""

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
from app.services import partners
from app.services.actors import Actor

router = APIRouter(tags=["fulfilment partner"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


class KindIn(BaseModel):
    kind: Literal["wholesale", "fulfilment_partner"]


class BranchIn(BaseModel):
    branch_id: uuid.UUID


class SourcingIn(BaseModel):
    branch_id: uuid.UUID
    online_product_id: uuid.UUID
    mode: Literal["own", "partner"]
    partner_id: uuid.UUID | None = None


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
                                      p.branch_scope())


@router.delete("/partners/{supplier_id}/branches/{branch_id}")
async def unlink_branch(supplier_id: uuid.UUID, branch_id: uuid.UUID, request: Request,
                        p: Principal = Depends(require("partners.write")),
                        db: AsyncSession = Depends(get_db)):
    return await partners.unlink_branch(db, _actor(p, request), supplier_id, branch_id,
                                        p.branch_scope())


@router.post("/partners/{supplier_id}/seed")
async def seed(supplier_id: uuid.UUID, body: BranchIn, request: Request,
               p: Principal = Depends(require("partners.write")),
               db: AsyncSession = Depends(get_db)):
    return await partners.seed(db, _actor(p, request), supplier_id, body.branch_id,
                               p.branch_scope())


@router.put("/partners/sourcing")
async def set_sourcing(body: SourcingIn, request: Request,
                       p: Principal = Depends(require("partners.write")),
                       db: AsyncSession = Depends(get_db)):
    return await partners.set_sourcing(db, _actor(p, request), branch_id=body.branch_id,
                                       online_product_id=body.online_product_id,
                                       mode=body.mode, partner_id=body.partner_id,
                                       scope=p.branch_scope())


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
    return await partners.price_changes(db, scope, branch_id=branch_id, unseen_only=not all)


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
    return await partners.my_items(db, sp.supplier_id, q=q, branch_id=branch_id,
                                   limit=limit, offset=offset)


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
