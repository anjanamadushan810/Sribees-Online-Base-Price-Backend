"""Fulfilment partner (app/services/partners.py, partner_requests.py): staff set-up, the
partner's own item list (price to us + stock) and the customer orders it packs."""

import uuid
from datetime import date
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
from app.services import (
    partner_books,
    partner_requests,
    partner_statements,
    partner_sync,
    partners,
)
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


class TickIn(BaseModel):
    line_id: uuid.UUID
    qty: int = Field(ge=0, le=1_000_000)
    expiry_date: date | None = None  # when staff see it on the pack (optional)


class TicksIn(BaseModel):
    lines: list[TickIn] = Field(default=[], max_length=500)

    def pairs(self) -> list[tuple]:
        return [(t.line_id, t.qty, t.expiry_date) for t in self.lines]


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


@router.get("/partners/requests/lookup")
async def lookup_request(code: str = Query(min_length=1, max_length=60),
                         p: Principal = Depends(require("partners.read")),
                         db: AsyncSession = Depends(get_db)):
    """The request a scanned bag slip names (order number or FR number)."""
    return await partner_books.lookup(db, p.branch_scope(), code)


@router.get("/partners/books")
async def books_status(p: Principal = Depends(require("partners.read")),
                       db: AsyncSession = Depends(get_db)):
    """What needs a person: days not booked, bags that went out unscanned, returns waiting,
    partner events that failed."""
    return await partner_books.status(db, p.branch_scope())


class ReasonIn(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


@router.post("/partners/requests/{request_id}/exclude")
async def exclude_request(request_id: uuid.UUID, body: ReasonIn, request: Request,
                          p: Principal = Depends(require("partners.write")),
                          db: AsyncSession = Depends(get_db)):
    """The owner takes a handed request out of the day order (booked by hand)."""
    return await partner_books.exclude(db, _actor(p, request), p.branch_scope(), request_id,
                                       body.reason)


@router.get("/partners/returns")
async def staff_returns(status: Literal["open", "waiting", "sent", "accepted"] | None = "open",
                        p: Principal = Depends(require("partners.read")),
                        db: AsyncSession = Depends(get_db)):
    return await partner_books.staff_returns(db, p.branch_scope(), status=status)


@router.post("/partners/returns/{return_id}/sent")
async def return_sent(return_id: uuid.UUID, request: Request,
                      p: Principal = Depends(require("grn.create")),
                      db: AsyncSession = Depends(get_db)):
    """Staff handed the goods back to the partner."""
    return await partner_books.mark_sent(db, _actor(p, request), p.branch_scope(), return_id)


@router.get("/partners/failures")
async def failures(p: Principal = Depends(require("partners.read")),
                   db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import PartnerEventFailure  # noqa: PLC0415

    stmt = select(PartnerEventFailure).where(PartnerEventFailure.resolved_at.is_(None))
    scope = p.branch_scope()
    if scope is not None:
        stmt = stmt.where(PartnerEventFailure.branch_id.in_(scope))
    rows = await db.scalars(stmt.order_by(PartnerEventFailure.failure_id).limit(200))
    return [{"failure_id": f.failure_id, "seq": f.seq, "event_type": f.event_type,
             "order_id": str(f.order_id) if f.order_id else None,
             "branch_id": str(f.branch_id) if f.branch_id else None, "error": f.error,
             "created_at": f.created_at} for f in rows]


@router.post("/partners/failures/{failure_id}/retry")
async def retry_failure(failure_id: int, request: Request,
                        p: Principal = Depends(require("partners.write")),
                        db: AsyncSession = Depends(get_db)):
    """Run the partner part of a failed Online event again, on the order as it is NOW (a
    stale 'confirmed' must not bring back an order cancelled since: review 2 #2)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    from app.models import PartnerEventFailure  # noqa: PLC0415

    row = await db.get(PartnerEventFailure, failure_id, with_for_update=True)
    scope = p.branch_scope()
    if row is None or row.resolved_at is not None or (
            scope is not None and row.branch_id not in scope):
        raise HTTPException(status_code=404, detail="failure not found")
    ev = dict(row.payload)
    payload = dict(ev.get("payload") or {})
    moved_on = False
    # Only a live-status failure (a request to create or follow) is replayed on the order
    # as it is now; a cancel, RTO or return is replayed as it happened (review round 3 #1)
    if (ev.get("type") == "order.status" and payload.get("order_id")
            and payload.get("to_status") in partner_requests.LIVE):
        current = await partner_requests.fetch_order(uuid.UUID(str(payload["order_id"])))
        if current is None:
            raise HTTPException(status_code=409,
                                detail="SRIBEESonline cannot be reached; try again later")
        moved_on = current.get("status") != payload.get("to_status")
        payload["to_status"] = current.get("status") or payload.get("to_status")
        payload["lines"] = current.get("lines") or []
        ev["payload"] = payload
    try:
        async with db.begin_nested():
            what = await partner_requests.apply_event(db, ev)
    except Exception as exc:  # noqa: BLE001 -- shown to staff, kept to retry again
        row.error = str(getattr(exc, "message", exc))[:2000]
        await db.flush()
        await db.commit()
        raise HTTPException(status_code=409, detail=row.error) from None
    if what == "ignored" and moved_on and payload["to_status"] not in ("cancelled",):
        # the order moved on (e.g. went out) without the partner: keep it for staff
        row.error = (f"the order is now {payload['to_status']}; nothing was done for the "
                     "partner. Check it by hand.")
        await db.flush()
        await db.commit()
        raise HTTPException(status_code=409, detail=row.error)
    row.resolved_at, row.resolved_by = datetime.now(UTC), p.user_id
    return {"failure_id": failure_id, "result": what}


@router.get("/partners/statements")
async def staff_statements(branch_id: uuid.UUID, supplier_id: uuid.UUID | None = None,
                           week: date | None = None,
                           p: Principal = Depends(require("partners.read")),
                           db: AsyncSession = Depends(get_db)):
    """A week's statement (Monday-Sunday), or the last weeks' totals without `week`."""
    return await partner_statements.staff_view(db, p.branch_scope(), branch_id=branch_id,
                                               supplier_id=supplier_id, week=week)


@router.post("/partners/requests/{request_id}/tick")
async def tick_request(request_id: uuid.UUID, body: TicksIn, request: Request,
                       p: Principal = Depends(require("grn.create")),
                       db: AsyncSession = Depends(get_db)):
    return await partner_books.tick(db, _actor(p, request), p.branch_scope(), request_id,
                                    body.pairs())


@router.post("/partners/requests/{request_id}/handover")
async def handover_request(request_id: uuid.UUID, body: TicksIn, request: Request,
                           p: Principal = Depends(require("grn.create")),
                           db: AsyncSession = Depends(get_db)):
    """Take the bag (only complete: owner F20)."""
    return await partner_books.handover(db, _actor(p, request), p.branch_scope(), request_id,
                                        body.pairs())


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


@router.get("/supplier/partner/returns")
async def my_returns(open_only: bool = True,
                     sp: SupplierPrincipal = Depends(current_supplier),
                     db: AsyncSession = Depends(get_db)):
    await partners.my_links(db, sp.supplier_id)
    return await partner_books.partner_returns(db, sp.supplier_id, open_only=open_only)


@router.post("/supplier/partner/returns/{return_id}/accept")
async def accept_return(return_id: uuid.UUID,
                        sp: SupplierPrincipal = Depends(current_supplier),
                        db: AsyncSession = Depends(get_db)):
    """The partner has its goods back."""
    await partners.my_links(db, sp.supplier_id)
    return await partner_books.accept(db, sp.supplier_id, sp.user.supplier_user_id, return_id)


@router.get("/supplier/partner/statements")
async def my_statements(week: date | None = None, branch_id: uuid.UUID | None = None,
                        sp: SupplierPrincipal = Depends(current_supplier),
                        db: AsyncSession = Depends(get_db)):
    return await partner_statements.partner_view(db, sp.supplier_id, branch_id=branch_id,
                                                 week=week)


@router.post("/supplier/partner/statements/{week}/confirm")
async def confirm_statement(week: date, branch_id: uuid.UUID | None = None,
                            sp: SupplierPrincipal = Depends(current_supplier),
                            db: AsyncSession = Depends(get_db)):
    return await partner_statements.confirm(db, sp.supplier_id, sp.user.supplier_user_id,
                                            week=week, branch_id=branch_id)
