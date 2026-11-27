"""Product barcodes: look one up when staff scan a pack, link it the first time."""

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, require
from app.core.db import get_db
from app.core.permissions import has_permission
from app.services import barcodes
from app.services.actors import Actor

router = APIRouter(tags=["barcodes"])


def _actor(p: Principal, request: Request) -> Actor:
    return Actor(user_id=p.user_id, role=p.role, ip_address=client_ip(request))


class LinkIn(BaseModel):
    barcode: str = Field(min_length=8, max_length=20)
    online_variant_id: uuid.UUID


@router.get("/barcodes/{code}")
async def lookup(code: str, _: Principal = Depends(require("grn.create")),
                 db: AsyncSession = Depends(get_db)):
    return await barcodes.lookup(db, code)


@router.get("/online-items/{variant_id}/barcodes")
async def of_item(variant_id: uuid.UUID, _: Principal = Depends(require("products.read")),
                  db: AsyncSession = Depends(get_db)):
    return await barcodes.barcodes_of(db, variant_id)


@router.post("/barcodes")
async def link(body: LinkIn, request: Request, p: Principal = Depends(require("grn.create")),
               db: AsyncSession = Depends(get_db)):
    return await barcodes.link(db, _actor(p, request), raw=body.barcode,
                               online_variant_id=body.online_variant_id,
                               can_move=has_permission(p.role, "products.write"))


@router.delete("/barcodes/{code}", status_code=204)
async def unlink(code: str, request: Request, p: Principal = Depends(require("grn.create")),
                 db: AsyncSession = Depends(get_db)):
    await barcodes.unlink(db, _actor(p, request), code,
                          allowed=has_permission(p.role, "products.write"))
    return Response(status_code=204)
