"""Internal: SRIBEESonline's packing calls (``/internal/online/*``, phase E).

Online's staff scan a unit's QR label while packing; Online asks here at once (the packer
must know straight away when a label is wrong). Same three locks as /internal/finance/*:
a service token (only its SHA-256 here, ONLINE_PICK_TOKEN_SHA256; empty = 404), never
through the edge (a forwarded request is refused), and an optional IP allow-list.
"""

import hashlib
import hmac
import ipaddress
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.services import picks

router = APIRouter(prefix="/internal/online", include_in_schema=False)


def _allowed(peer: str) -> bool:
    raw = (get_settings().online_pick_allowed_ips or "").strip()
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(part.strip(), strict=False)
               for part in raw.split(",") if part.strip())


def require_online_service(request: Request) -> None:
    expected = (get_settings().online_pick_token_sha256 or "").strip().lower()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        raise HTTPException(status_code=403, detail="not available from outside")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    presented = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if not token or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="service token required")
    peer = request.client.host if request.client else ""
    if not _allowed(peer):
        raise HTTPException(status_code=403, detail="caller not allowed")


class PickIn(BaseModel):
    online_order_id: uuid.UUID
    order_number: str | None = Field(default=None, max_length=40)
    order_item_id: uuid.UUID
    online_branch_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    quantity: int = Field(gt=0, le=100_000)
    weight_grams: int | None = Field(default=None, gt=0)
    label: str = Field(min_length=1, max_length=40)
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=3)
    picked_by: str | None = Field(default=None, max_length=120)


class UnpickIn(BaseModel):
    online_order_id: uuid.UUID
    label: str = Field(min_length=1, max_length=40)


@router.post("/pick")
async def pick(body: PickIn, _: None = Depends(require_online_service),
               db: AsyncSession = Depends(get_db)) -> dict:
    return await picks.pick(
        db, order_id=body.online_order_id, order_number=body.order_number,
        item_id=body.order_item_id, online_branch_id=body.online_branch_id,
        variant_id=body.variant_id, ordered_qty=body.quantity, weight_grams=body.weight_grams,
        raw_label=body.label, weight_kg=body.weight_kg, picked_by=body.picked_by)


@router.post("/unpick")
async def unpick(body: UnpickIn, _: None = Depends(require_online_service),
                 db: AsyncSession = Depends(get_db)) -> dict:
    await picks.unpick(db, order_id=body.online_order_id, raw_label=body.label)
    return await picks.order_summary(db, body.online_order_id)


@router.get("/orders/{online_order_id}/picks")
async def order_picks(online_order_id: uuid.UUID, _: None = Depends(require_online_service),
                      db: AsyncSession = Depends(get_db)) -> dict:
    return await picks.order_summary(db, online_order_id)
