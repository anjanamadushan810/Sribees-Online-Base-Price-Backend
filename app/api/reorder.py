"""Automatic draft orders: run now, what the last run did, main supplier per item."""

import uuid

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require, scoped_branches
from app.core.db import get_db
from app.models import Branch
from app.services import reorder
from app.services.errors import NotFound

router = APIRouter(tags=["reorder"])


class RunIn(BaseModel):
    branch_id: uuid.UUID | None = None


class MainSupplierIn(BaseModel):
    online_variant_id: uuid.UUID
    supplier_id: uuid.UUID | None = None  # None = back to the last supplier we bought from


@router.get("/reorder")
async def last_runs(p: Principal = Depends(require("orders.read")),
                    db: AsyncSession = Depends(get_db)):
    return await reorder.latest(db, scoped_branches(p, None))


@router.post("/reorder/run")
async def run_now(body: RunIn, p: Principal = Depends(require("orders.create")),
                  db: AsyncSession = Depends(get_db)):
    found = await reorder.branches(db, scoped_branches(p, body.branch_id))
    if body.branch_id is not None and not found:
        raise NotFound("branch not found or not linked to SRIBEESonline")
    out = {}
    async with httpx.AsyncClient() as client:
        for b in found:
            branch = await db.get(Branch, b.branch_id)
            out[str(b.branch_id)] = await reorder.run_branch(db, client, branch, user_id=p.user_id)
    return out


@router.put("/reorder/main-supplier", status_code=204)
async def main_supplier(body: MainSupplierIn, p: Principal = Depends(require("orders.create")),
                        db: AsyncSession = Depends(get_db)):
    await reorder.set_main_supplier(db, body.online_variant_id, body.supplier_id,
                                    user_id=p.user_id)
