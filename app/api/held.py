"""Held stock (owner 2026-11-27): goods kept aside without QR labels, and taking some of
them into the shop by sticking labels on (app/services/held.py)."""

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require, scoped_branches
from app.core.db import get_db
from app.services import held, labels
from app.services.actors import Actor

router = APIRouter(tags=["held stock"])


class ReleaseIn(BaseModel):
    numbers: list[str] = Field(default=[], max_length=1000)
    # or a strip of labels: the next `count` unused ones from `start`
    start: str | None = None
    count: int | None = Field(default=None, gt=0, le=1000)


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


@router.get("/stock/held")
async def held_stock(branch_id: uuid.UUID | None = None,
                     p: Principal = Depends(require("stock.read")),
                     db: AsyncSession = Depends(get_db)):
    scope = scoped_branches(p, branch_id)
    lines = await held.held_lines(db, scope)
    return {"summary": {"lines": len(lines), "units": sum(r["left"] for r in lines),
                        "oldest_days": max((r["days"] for r in lines), default=0)},
            "lines": lines, "releases": await held.releases(db, scope)}


@router.get("/stock/held/summary")
async def held_summary(branch_id: uuid.UUID | None = None,
                       p: Principal = Depends(require("stock.read")),
                       db: AsyncSession = Depends(get_db)):
    return await held.summary(db, scoped_branches(p, branch_id))


@router.post("/stock/held/{receipt_line_id}/release")
async def release(receipt_line_id: uuid.UUID, body: ReleaseIn, request: Request,
                  p: Principal = Depends(require("grn.create")),
                  db: AsyncSession = Depends(get_db)):
    numbers = list(body.numbers)
    if body.start is not None:
        if numbers or body.count is None:
            from fastapi import HTTPException  # noqa: PLC0415
            raise HTTPException(status_code=422, detail="give either numbers, or start with count")
        numbers = await labels.next_free(db, start=body.start, count=body.count)
    return await held.release(db, _actor(p, request), receipt_line_id, numbers=numbers,
                              scope=scoped_branches(p, None))


@router.post("/stock/held/releases/{release_id}/retry", status_code=204)
async def retry(release_id: uuid.UUID, request: Request,
                p: Principal = Depends(require("grn.create")),
                db: AsyncSession = Depends(get_db)):
    await held.retry(db, _actor(p, request), release_id, scoped_branches(p, None))
    return Response(status_code=204)
