"""QR labels: print a run (PDF of A4 sticker sheets), link labels to a batch, look one up."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.core.db import get_db
from app.models import Label, LabelRange, StockBatch
from app.services import label_pdf, labels
from app.services.actors import Actor

router = APIRouter(tags=["labels"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


class RangeIn(BaseModel):
    count: int = Field(gt=0, le=labels.MAX_RANGE)
    branch_id: uuid.UUID | None = None


class RangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    range_id: uuid.UUID
    range_no: str
    count: int
    first: str
    last: str
    branch_id: uuid.UUID | None
    cols: int
    rows: int
    sheets: int
    linked: int = 0
    created_at: datetime


class LinkIn(BaseModel):
    numbers: list[str] = Field(default=[], max_length=labels.MAX_LINK)
    # or "the next N unused labels of a run, from this one"
    start: str | None = None
    count: int | None = Field(default=None, gt=0, le=labels.MAX_LINK)


class LinkOut(BaseModel):
    batch_id: uuid.UUID
    linked: list[str]
    labels_on_batch: int


class LabelBatch(BaseModel):
    batch_id: uuid.UUID
    batch_no: str
    branch_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    unit: str
    received_date: date
    expiry_date: date | None
    qty_on_hand: Decimal
    status: str


class LabelOut(BaseModel):
    number: str
    status: str
    range_no: str
    linked_at: datetime | None
    used_at: datetime | None
    used_ref: str | None
    batch: LabelBatch | None


def _range_out(r: LabelRange, linked: int = 0) -> RangeOut:
    per = r.cols * r.rows
    return RangeOut(range_id=r.range_id, range_no=r.range_no, count=r.count,
                    first=labels.make_number(r.first_body), last=labels.make_number(r.last_body),
                    branch_id=r.branch_id, cols=r.cols, rows=r.rows,
                    sheets=(r.count + per - 1) // per, linked=linked, created_at=r.created_at)


@router.post("/labels/ranges", response_model=RangeOut, status_code=201)
async def print_labels(body: RangeIn, request: Request,
                       p: Principal = Depends(require("labels.print")),
                       db: AsyncSession = Depends(get_db)):
    if body.branch_id is not None and not p.can_see_branch(body.branch_id):
        raise HTTPException(status_code=404, detail="not found")
    rng = await labels.register_range(db, _actor(p, request), count=body.count,
                                      branch_id=body.branch_id)
    return _range_out(rng)


@router.get("/labels/ranges", response_model=list[RangeOut])
async def list_ranges(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, le=200),
                      p: Principal = Depends(require("stock.read")),
                      db: AsyncSession = Depends(get_db)):
    q = select(LabelRange).order_by(LabelRange.created_at.desc()).offset(offset).limit(limit)
    branches = scoped_branches(p, None)
    if branches is not None:
        q = q.where((LabelRange.branch_id.is_(None)) | LabelRange.branch_id.in_(branches))
    rows = list(await db.scalars(q))
    counts = dict((await db.execute(
        select(Label.range_id, func.count()).where(
            Label.range_id.in_([r.range_id for r in rows]), Label.status != "printed")
        .group_by(Label.range_id))).all()) if rows else {}
    return [_range_out(r, counts.get(r.range_id, 0)) for r in rows]


@router.get("/labels/ranges/{range_id}/pdf")
async def range_pdf(range_id: uuid.UUID, p: Principal = Depends(require("labels.print")),
                    db: AsyncSession = Depends(get_db)):
    rng = await db.get(LabelRange, range_id)
    if rng is None or (rng.branch_id is not None and not p.can_see_branch(rng.branch_id)):
        raise HTTPException(status_code=404, detail="not found")
    pdf = label_pdf.build_pdf(labels.range_numbers(rng), cols=rng.cols, rows=rng.rows,
                              title=f"Sribees labels {rng.range_no} ({rng.count})")
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{rng.range_no}.pdf"',
        "Cache-Control": "no-store"})


@router.get("/labels/{number}", response_model=LabelOut)
async def label_lookup(number: str, p: Principal = Depends(require("stock.read")),
                       db: AsyncSession = Depends(get_db)):
    return await labels.lookup(db, number, scoped_branches(p, None))


@router.post("/stock/batches/{batch_id}/labels", response_model=LinkOut)
async def link_labels(batch_id: uuid.UUID, body: LinkIn, request: Request,
                      p: Principal = Depends(require("grn.create")),
                      db: AsyncSession = Depends(get_db)):
    batch = await db.get(StockBatch, batch_id, with_for_update=True)
    if batch is None or not p.can_see_branch(batch.branch_id):
        raise HTTPException(status_code=404, detail="not found")
    numbers = list(body.numbers)
    if body.start is not None:
        if numbers or body.count is None:
            raise HTTPException(status_code=422,
                                detail="give either numbers, or start with count")
        numbers = await labels.next_free(db, start=body.start, count=body.count)
    res = await labels.link(db, _actor(p, request), batch=batch, numbers=numbers)
    return LinkOut(batch_id=res.batch_id, linked=res.linked, labels_on_batch=res.labels_on_batch)
