"""Write-offs, branch transfers and stock counts."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.api.stock_schemas import (
    AdjustmentOut,
    CountEntryIn,
    CountLineOut,
    CountOut,
    CountStartIn,
    ReasonIn,
    TransferIn,
    TransferOut,
    TransferReceiveIn,
    WriteOffIn,
)
from app.core.db import get_db
from app.models import Product, StockAdjustment, StockBatch, StockCount, StockTransfer
from app.services import stock_control
from app.services.actors import Actor

router = APIRouter(tags=["stock"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


def _check_branch(p: Principal, *branch_ids: uuid.UUID) -> None:
    if not any(p.can_see_branch(b) for b in branch_ids):
        raise HTTPException(status_code=404, detail="not found")


def _lines(lines) -> list[stock_control.BatchQtyIn]:
    return [stock_control.BatchQtyIn(**ln.model_dump()) for ln in lines]


# ---------------------------------------------------------------- adjustments
@router.get("/stock/adjustments", response_model=list[AdjustmentOut])
async def list_adjustments(
    status: str | None = None,
    branch_id: uuid.UUID | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    q = (select(StockAdjustment)
         .order_by(StockAdjustment.adjustment_date.desc(), StockAdjustment.adjustment_no.desc())
         .offset(offset).limit(limit))
    branches = scoped_branches(p, branch_id)
    if branches is not None:
        q = q.where(StockAdjustment.branch_id.in_(branches))
    if status:
        q = q.where(StockAdjustment.status == status)
    return list(await db.scalars(q))


@router.post("/stock/write-offs", response_model=AdjustmentOut, status_code=201)
async def request_writeoff(
    body: WriteOffIn,
    request: Request,
    p: Principal = Depends(require("stock.adjust")),
    db: AsyncSession = Depends(get_db),
):
    _check_branch(p, body.branch_id)
    return await stock_control.request_writeoff(
        db, _actor(p, request), branch_id=body.branch_id, reason=body.reason,
        adjustment_date=body.adjustment_date, notes=body.notes, lines=_lines(body.lines),
    )


@router.post("/stock/adjustments/{adjustment_id}/approve", response_model=AdjustmentOut)
async def approve_adjustment(
    adjustment_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(require("stock.approve")),
    db: AsyncSession = Depends(get_db),
):
    adj = await stock_control.get_adjustment(db, adjustment_id, lock=True)
    return await stock_control.approve_adjustment(db, _actor(p, request), adj)


@router.post("/stock/adjustments/{adjustment_id}/reject", response_model=AdjustmentOut)
async def reject_adjustment(
    adjustment_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    p: Principal = Depends(require("stock.approve")),
    db: AsyncSession = Depends(get_db),
):
    adj = await stock_control.get_adjustment(db, adjustment_id, lock=True)
    return await stock_control.reject_adjustment(db, _actor(p, request), adj, reason=body.reason)


# ---------------------------------------------------------------- transfers
@router.get("/stock/transfers", response_model=list[TransferOut])
async def list_transfers(
    status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    q = (select(StockTransfer)
         .order_by(StockTransfer.shipped_date.desc(), StockTransfer.transfer_no.desc())
         .offset(offset).limit(limit))
    scope = p.branch_scope()
    if scope is not None:
        q = q.where(StockTransfer.from_branch_id.in_(scope)
                    | StockTransfer.to_branch_id.in_(scope))
    if status:
        q = q.where(StockTransfer.status == status)
    return list(await db.scalars(q))


@router.post("/stock/transfers", response_model=TransferOut, status_code=201)
async def ship_transfer(
    body: TransferIn,
    request: Request,
    p: Principal = Depends(require("stock.transfer")),
    db: AsyncSession = Depends(get_db),
):
    _check_branch(p, body.from_branch_id)
    return await stock_control.ship_transfer(
        db, _actor(p, request), from_branch_id=body.from_branch_id,
        to_branch_id=body.to_branch_id, shipped_date=body.shipped_date, notes=body.notes,
        lines=_lines(body.lines),
    )


@router.post("/stock/transfers/{transfer_id}/receive", response_model=TransferOut)
async def receive_transfer(
    transfer_id: uuid.UUID,
    body: TransferReceiveIn,
    request: Request,
    p: Principal = Depends(require("stock.transfer")),
    db: AsyncSession = Depends(get_db),
):
    transfer = await stock_control.get_transfer(db, transfer_id, lock=True)
    _check_branch(p, transfer.to_branch_id)
    return await stock_control.receive_transfer(
        db, _actor(p, request), transfer, received_date=body.received_date,
        received={ln.transfer_line_id: ln.qty_received for ln in body.lines},
    )


# ---------------------------------------------------------------- counts
async def _count_out(db: AsyncSession, count: StockCount) -> CountOut:
    rows = {b.batch_id: (b, name) for b, name in await db.execute(
        select(StockBatch, Product.name)
        .join(Product, Product.product_id == StockBatch.product_id)
        .where(StockBatch.batch_id.in_([ln.batch_id for ln in count.lines])))}
    lines = []
    for ln in count.lines:
        batch, name = rows[ln.batch_id]
        lines.append(CountLineOut.model_validate(ln).model_copy(update={
            "batch_no": batch.batch_no, "product_id": batch.product_id, "product_name": name,
            "expiry_date": batch.expiry_date}))
    lines.sort(key=lambda x: (x.product_name.lower(), x.batch_no))
    out = CountOut.model_validate(count)
    out.lines = lines
    return out


async def _load_count(db, p: Principal, count_id: uuid.UUID, lock=False) -> StockCount:
    count = await stock_control.get_count(db, count_id, lock=lock)
    _check_branch(p, count.branch_id)
    return count


@router.post("/stock/counts", response_model=CountOut, status_code=201)
async def start_count(
    body: CountStartIn,
    request: Request,
    p: Principal = Depends(require("stock.adjust")),
    db: AsyncSession = Depends(get_db),
):
    _check_branch(p, body.branch_id)
    count = await stock_control.start_count(
        db, _actor(p, request), branch_id=body.branch_id, count_date=body.count_date,
        product_ids=body.product_ids, notes=body.notes,
    )
    return await _count_out(db, count)


@router.get("/stock/counts/{count_id}", response_model=CountOut)
async def get_count(
    count_id: uuid.UUID,
    p: Principal = Depends(require("stock.read")),
    db: AsyncSession = Depends(get_db),
):
    return await _count_out(db, await _load_count(db, p, count_id))


@router.post("/stock/counts/{count_id}/lines", response_model=CountOut)
async def enter_counts(
    count_id: uuid.UUID,
    body: list[CountEntryIn],
    p: Principal = Depends(require("stock.adjust")),
    db: AsyncSession = Depends(get_db),
):
    count = await _load_count(db, p, count_id, lock=True)
    count = await stock_control.enter_counts(
        db, count, {ln.batch_id: ln.counted_qty for ln in body})
    return await _count_out(db, count)


@router.post("/stock/counts/{count_id}/submit", response_model=CountOut)
async def submit_count(
    count_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(require("stock.adjust")),
    db: AsyncSession = Depends(get_db),
):
    count = await _load_count(db, p, count_id, lock=True)
    return await _count_out(db, await stock_control.submit_count(db, _actor(p, request), count))


@router.post("/stock/counts/{count_id}/cancel", response_model=CountOut)
async def cancel_count(
    count_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(require("stock.adjust")),
    db: AsyncSession = Depends(get_db),
):
    count = await _load_count(db, p, count_id, lock=True)
    return await _count_out(db, await stock_control.cancel_count(db, _actor(p, request), count))
