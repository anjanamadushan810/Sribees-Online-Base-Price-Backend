"""Mirror of SRIBEESonline's product list (read-only here).

Contract (Online side NOT built yet; see QUESTIONS.md): GET {ONLINE_API_URL}/internal/
supply/catalog with "Authorization: Bearer {ONLINE_SERVICE_TOKEN}" answers

    {"products": [{"product_id", "sku", "name", "name_si", "display_size",
                   "display_size_unit", "is_active", "image_url", "weight_grams",
                   "variants": [{"variant_id", "sku", "name", "weight_grams",
                                 "is_active"}]}],
     "branches": [{"branch_id", "code", "name", "is_active"}]}   # branches: optional

A product may also carry "listings": [{"branch_id", "sourcing_group"}], the Online branches
that sell it and how each gets it ('store' / 'external_daily' / 'external_slow' / null;
fulfilment partner, migration 0030). When no product carries the key (an older Online) the
listings are left alone.

Branches are Online's: a branch missing from the answer is left alone (never deleted or
switched off by a sync); one we already have by code is linked, not duplicated.

Same pattern as Online's /internal/finance/* (token hash only on Online, internal
network only). A product missing from a sync is marked inactive, never deleted.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
from sqlalchemy import delete, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.matching.text import parse_size
from app.models import (
    Branch,
    OnlineBranchListing,
    OnlineProduct,
    OnlineVariant,
    SyncState,
)
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


def _image(url) -> str | None:
    """Only a plain web address is kept (it is shown in an <img>)."""
    if isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) <= 500:
        return url
    return None


def _grams(size: tuple[Decimal, str] | None) -> int | None:
    """Weight of a default pack size from the product's size (1 ml = 1 g, as for couriers)."""
    if size is None or size[1] not in ("mass", "volume") or size[0] <= 0:
        return None
    return int(size[0])


def _weight(grams) -> int | None:
    return grams if isinstance(grams, int) and 0 < grams < 10_000_000 else None


async def apply_branches(db: AsyncSession, branches: list) -> dict:
    """Make Supply's branches follow Online's. Matched by Online's id; a branch typed in by
    hand with the same code and no Online id yet is linked to it instead of duplicated."""
    out = {"created": 0, "linked": 0, "updated": 0, "skipped": 0}
    for b in branches:
        try:
            bid = uuid.UUID(str(b["branch_id"]))
        except (KeyError, ValueError):
            out["skipped"] += 1
            continue
        code = (b.get("code") or "").strip()[:20]
        name = (b.get("name") or "").strip()[:120]
        active = bool(b.get("is_active", True))
        if not code or not name:
            out["skipped"] += 1
            continue
        row = await db.scalar(select(Branch).where(Branch.online_branch_id == bid))
        if row is not None:
            if (row.name, row.is_active) != (name, active):
                row.name, row.is_active = name, active
                out["updated"] += 1
            continue
        same_code = await db.scalar(select(Branch).where(Branch.code == code))
        if same_code is not None:
            if same_code.online_branch_id is None:
                same_code.online_branch_id = bid
                out["linked"] += 1
            else:
                out["skipped"] += 1  # the code belongs to a different Online branch
            continue
        db.add(Branch(code=code, name=name, online_branch_id=bid, is_active=active))
        out["created"] += 1
    await db.flush()
    return out


def _days(v) -> int | None:
    """Online's expiry alert days (1..365); anything else = the default."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 365 else None


async def apply(db: AsyncSession, payload: dict, *, user_id: uuid.UUID | None) -> dict:
    products = payload.get("products")
    if not isinstance(products, list):
        raise SupplyError("SRIBEESonline sent no product list")
    if not products and await db.scalar(select(OnlineProduct.online_product_id).limit(1)):
        # An empty answer would switch every product off. The worker syncs by itself now:
        # never trust an empty list when we already hold products.
        raise SupplyError("SRIBEESonline sent an empty product list; nothing was changed")
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
            image_url=_image(p.get("image_url")),
            weight_grams=_weight(p.get("weight_grams")),
            expiry_alert_days=_days(p.get("expiry_alert_days")),
            stock_mode=(p.get("stock_mode") if p.get("stock_mode") in ("unit", "loose", "packed")
                        else None),
        )
        stmt = insert(OnlineProduct).values(online_product_id=pid, **values)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[OnlineProduct.online_product_id],
            set_={**values, "synced_at": stmt.excluded.synced_at}))
        variants = p.get("variants") or []
        for v in variants:
            vvalues = dict(online_product_id=pid, sku=v.get("sku"),
                           name=(v.get("name") or "")[:120],
                           weight_grams=v.get("weight_grams"),
                           is_active=bool(v.get("is_active", True)))
            vstmt = insert(OnlineVariant).values(
                online_variant_id=uuid.UUID(v["variant_id"]), **vvalues)
            await db.execute(vstmt.on_conflict_do_update(
                index_elements=[OnlineVariant.online_variant_id], set_=vvalues))
        # A product sold without pack sizes (most of Online's) still needs one to hold stock:
        # its default pack size has the product's own id. Owner 2026-11-26. It is switched
        # off (never deleted: stock may sit on it) once the product gets real pack sizes.
        dvalues = dict(online_product_id=pid, sku=p.get("sku"), name="",
                       weight_grams=_grams(size), is_active=not variants)
        dstmt = insert(OnlineVariant).values(online_variant_id=pid, **dvalues)
        if variants:
            await db.execute(update(OnlineVariant).where(OnlineVariant.online_variant_id == pid)
                             .values(is_active=False))
        else:
            await db.execute(dstmt.on_conflict_do_update(
                index_elements=[OnlineVariant.online_variant_id], set_=dvalues))
    gone = await db.execute(
        update(OnlineProduct)
        .where(OnlineProduct.online_product_id.not_in(seen) if seen else true(),
               OnlineProduct.is_active.is_(True))
        .values(is_active=False))
    total = len(seen)
    state = await db.get(SyncState, "online_catalog")
    value = {"products": total, "deactivated": gone.rowcount}
    if isinstance(payload.get("branches"), list):
        value["branches"] = await apply_branches(db, payload["branches"])
    listed = await apply_listings(db, products)
    if listed is not None:
        value["listings"] = listed
    if state is None:
        db.add(SyncState(key="online_catalog", value=value))
    else:
        state.value = value
    await audit.record(db, user_id=user_id, action="online.catalog_sync",
                       entity="online_catalog", after=value)
    await db.flush()
    return value


GROUPS = ("store", "external_daily", "external_slow")


async def apply_listings(db: AsyncSession, products: list) -> int | None:
    """Which branch sells which product and how it gets it. Only the products in this answer
    that carry "listings" are rewritten; None = Online sent none (older Online)."""
    carrying = [p for p in products if isinstance(p.get("listings"), list)]
    if not carrying:
        return None
    rows = await db.execute(select(Branch.online_branch_id, Branch.branch_id)
                            .where(Branch.online_branch_id.is_not(None)))
    ours = {str(o): b for o, b in rows.all()}
    pids = [uuid.UUID(p["product_id"]) for p in carrying]
    await db.execute(delete(OnlineBranchListing)
                     .where(OnlineBranchListing.online_product_id.in_(pids)))
    count = 0
    for p in carrying:
        pid = uuid.UUID(p["product_id"])
        groups: dict = {}
        for item in p["listings"]:
            bid = ours.get(str((item or {}).get("branch_id")))
            if bid is not None:
                group = item.get("sourcing_group")
                groups[bid] = group if group in GROUPS else None
        for bid, group in groups.items():
            db.add(OnlineBranchListing(branch_id=bid, online_product_id=pid,
                                       sourcing_group=group))
            count += 1
    await db.flush()
    return count


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
