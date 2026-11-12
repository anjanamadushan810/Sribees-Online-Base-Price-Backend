"""Mirror of SRIBEESonline's product list (read-only here).

Contract (Online side NOT built yet; see QUESTIONS.md): GET {ONLINE_API_URL}/internal/
supply/catalog with "Authorization: Bearer {ONLINE_SERVICE_TOKEN}" answers

    {"products": [{"product_id", "sku", "name", "name_si", "display_size",
                   "display_size_unit", "is_active",
                   "variants": [{"variant_id", "sku", "name", "weight_grams",
                                 "is_active"}]}]}

Same pattern as Online's /internal/finance/* (token hash only on Online, internal
network only). A product missing from a sync is marked inactive, never deleted.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
from sqlalchemy import select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.matching.text import parse_size
from app.models import OnlineProduct, OnlineVariant, SyncState
from app.services import audit
from app.services.errors import SupplyError


async def fetch() -> dict:
    s = get_settings()
    if not s.online_api_url or not s.online_service_token:
        raise SupplyError("the link to SRIBEESonline is not set up yet")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{s.online_api_url.rstrip('/')}/internal/supply/catalog",
                             headers={"Authorization": f"Bearer {s.online_service_token}"})
    if r.status_code != 200:
        raise SupplyError(f"SRIBEESonline answered {r.status_code}")
    return r.json()


def _size(p: dict) -> tuple[Decimal, str] | None:
    if p.get("display_size"):
        found = parse_size(f"{p['display_size']}{p.get('display_size_unit') or ''}")
        if found:
            return found
    return parse_size(p.get("name") or "")


async def apply(db: AsyncSession, payload: dict, *, user_id: uuid.UUID | None) -> dict:
    products = payload.get("products")
    if not isinstance(products, list):
        raise SupplyError("SRIBEESonline sent no product list")
    seen: set[uuid.UUID] = set()
    for p in products:
        pid = uuid.UUID(p["product_id"])
        seen.add(pid)
        size = _size(p)
        values = dict(
            sku=p.get("sku"), name=(p.get("name") or "")[:255], name_si=p.get("name_si"),
            display_size=p.get("display_size"), display_size_unit=p.get("display_size_unit"),
            size_amount=size[0] if size else None, size_kind=size[1] if size else None,
            is_active=bool(p.get("is_active", True)),
        )
        stmt = insert(OnlineProduct).values(online_product_id=pid, **values)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[OnlineProduct.online_product_id],
            set_={**values, "synced_at": stmt.excluded.synced_at}))
        for v in p.get("variants") or []:
            vvalues = dict(online_product_id=pid, sku=v.get("sku"),
                           name=(v.get("name") or "")[:120],
                           weight_grams=v.get("weight_grams"),
                           is_active=bool(v.get("is_active", True)))
            vstmt = insert(OnlineVariant).values(
                online_variant_id=uuid.UUID(v["variant_id"]), **vvalues)
            await db.execute(vstmt.on_conflict_do_update(
                index_elements=[OnlineVariant.online_variant_id], set_=vvalues))
    gone = await db.execute(
        update(OnlineProduct)
        .where(OnlineProduct.online_product_id.not_in(seen) if seen else true(),
               OnlineProduct.is_active.is_(True))
        .values(is_active=False))
    total = len(seen)
    state = await db.get(SyncState, "online_catalog")
    value = {"products": total, "deactivated": gone.rowcount}
    if state is None:
        db.add(SyncState(key="online_catalog", value=value))
    else:
        state.value = value
    await audit.record(db, user_id=user_id, action="online.catalog_sync",
                       entity="online_catalog", after=value)
    await db.flush()
    return value


async def sync(db: AsyncSession, *, user_id: uuid.UUID | None) -> dict:
    return await apply(db, await fetch(), user_id=user_id)


async def search(db: AsyncSession, q: str, limit: int = 20) -> list[OnlineProduct]:
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = await db.scalars(
        select(OnlineProduct)
        .where(OnlineProduct.is_active.is_(True),
               (OnlineProduct.name.ilike(like)) | (OnlineProduct.name_si.ilike(like))
               | (OnlineProduct.sku == q))
        .order_by(OnlineProduct.name).limit(limit))
    return list(rows)
