"""Fulfilment partner: set-up and the partner's own figures
(docs/plans/fulfilment-partner-2026-11-27.md).

A fulfilment partner is a shop that fills, order by order, the products a branch sells but
does not carry. Which products those are is decided in SRIBEES Online (sourcing_group on the
branch's product: Our Store / Other Products); Supply mirrors it (online_branch_listings).
This module holds who the partner is and which branch it serves, and the partner's own
figures per pack size: its price to us and its stock count (partner_items).

Guardrails (plan G1-G7): nothing here changes an existing flow. A branch without a partner
behaves exactly as before. Nothing names a branch or a partner: Nimali/Matugama are data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Branch,
    OnlineBranchListing,
    OnlineProduct,
    OnlineVariant,
    PartnerItem,
    PartnerPriceChange,
    Supplier,
    SupplierBranch,
)
from app.services import audit
from app.services.actors import Actor, _now
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

PARTNER = "fulfilment_partner"
WHOLESALE = "wholesale"
KINDS = (WHOLESALE, PARTNER)
EXTERNAL = ("external_daily", "external_slow")  # Online's "Other Products"
UNSET = object()


def money(v: Decimal | None) -> str | None:
    """Money leaves as a string with two decimals (never a float)."""
    return None if v is None else str(Decimal(v).quantize(Decimal("0.01")))


def check_scope(scope: set[uuid.UUID] | None, branch_id: uuid.UUID) -> None:
    if scope is not None and branch_id not in scope:
        raise Forbidden("you do not have access to this branch")


async def _supplier(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFound("supplier not found")
    return supplier


async def _branch(db: AsyncSession, branch_id: uuid.UUID) -> Branch:
    branch = await db.get(Branch, branch_id)
    if branch is None or not branch.is_active:
        raise NotFound("branch not found")
    return branch


async def link_of_branch(db: AsyncSession, branch_id: uuid.UUID) -> SupplierBranch | None:
    """The branch's working partner link: a linked, active fulfilment partner, else None."""
    return await db.scalar(
        select(SupplierBranch).join(Supplier, Supplier.supplier_id == SupplierBranch.supplier_id)
        .where(SupplierBranch.branch_id == branch_id, Supplier.is_active.is_(True),
               Supplier.kind == PARTNER))


async def partner_branches(db: AsyncSession, supplier_id: uuid.UUID) -> dict:
    """{branch_id: SupplierBranch} for the branches this partner serves."""
    rows = await db.scalars(select(SupplierBranch)
                            .where(SupplierBranch.supplier_id == supplier_id))
    return {r.branch_id: r for r in rows}


def fills(group: str | None, link: SupplierBranch) -> str | None:
    """How the partner fills a product of this sourcing group: 'all' (Other Products),
    'shortfall' (Our Store, when the link says so) or None."""
    if group in EXTERNAL:
        return "all"
    if group == "store" and link.fills_store_shortfall:
        return "shortfall"
    return None


# ======================================================================= staff: set-up
async def set_kind(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID, kind: str) -> dict:
    if kind not in KINDS:
        raise SupplyError("kind must be wholesale or fulfilment_partner")
    supplier = await _supplier(db, supplier_id)
    if supplier.kind == kind:
        return {"supplier_id": str(supplier_id), "kind": kind}
    if kind == WHOLESALE and await partner_branches(db, supplier_id):
        raise Conflict("unlink the partner from its branch first")
    before = supplier.kind
    supplier.kind = kind
    await audit.record(db, user_id=actor.user_id, action="partner.kind", entity="supplier",
                       entity_id=supplier_id, before={"kind": before}, after={"kind": kind},
                       ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "kind": kind}


async def link_branch(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID,
                      branch_id: uuid.UUID, scope: set[uuid.UUID] | None, *,
                      fills_store_shortfall: bool = False) -> dict:
    check_scope(scope, branch_id)
    supplier = await _supplier(db, supplier_id)
    if supplier.kind != PARTNER:
        raise SupplyError("this supplier is not a fulfilment partner")
    if not supplier.is_active:
        raise SupplyError("this partner is switched off")
    await _branch(db, branch_id)
    current = await db.scalar(select(SupplierBranch).where(SupplierBranch.branch_id == branch_id))
    if current is not None and current.supplier_id != supplier_id:
        raise Conflict("this branch already has a fulfilment partner; unlink it first")
    await db.execute(insert(SupplierBranch)
                     .values(supplier_id=supplier_id, branch_id=branch_id,
                             fills_store_shortfall=fills_store_shortfall,
                             created_by=actor.user_id)
                     .on_conflict_do_update(
                         index_elements=[SupplierBranch.supplier_id, SupplierBranch.branch_id],
                         set_={"fills_store_shortfall": fills_store_shortfall}))
    await audit.record(db, user_id=actor.user_id, action="partner.link_branch",
                       entity="supplier", entity_id=supplier_id,
                       after={"branch_id": str(branch_id),
                              "fills_store_shortfall": fills_store_shortfall},
                       ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "branch_id": str(branch_id),
            "fills_store_shortfall": fills_store_shortfall}


async def unlink_branch(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID,
                        branch_id: uuid.UUID, scope: set[uuid.UUID] | None) -> dict:
    """Clean exit (G6): from now on the branch's orders create no partner requests and the
    partner app shows nothing for it. Requests already open finish normally. Its figures
    stay for history."""
    check_scope(scope, branch_id)
    await _supplier(db, supplier_id)
    link = await db.execute(delete(SupplierBranch).where(
        SupplierBranch.supplier_id == supplier_id, SupplierBranch.branch_id == branch_id))
    if not link.rowcount:
        raise NotFound("this partner does not serve this branch")
    await audit.record(db, user_id=actor.user_id, action="partner.unlink_branch",
                       entity="supplier", entity_id=supplier_id,
                       after={"branch_id": str(branch_id)}, ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "branch_id": str(branch_id)}


# ======================================================================= staff: reading
def _group_filter(stmt, mode: str | None):
    group = OnlineBranchListing.sourcing_group
    if mode == "partner":
        return stmt.where(group.in_(EXTERNAL))
    if mode == "own":
        return stmt.where(or_(group.is_(None), group.not_in(EXTERNAL)))
    return stmt


async def sourcing_list(db: AsyncSession, branch_id: uuid.UUID, scope: set[uuid.UUID] | None,
                        *, mode: str | None = None, q: str | None = None,
                        limit: int = 50, offset: int = 0) -> dict:
    """The branch's products and how each is got (read only: decided in Online's admin)."""
    check_scope(scope, branch_id)
    stmt = (select(OnlineProduct, OnlineBranchListing.sourcing_group)
            .select_from(OnlineBranchListing)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == OnlineBranchListing.online_product_id)
            .where(OnlineBranchListing.branch_id == branch_id))
    stmt = _group_filter(stmt, mode)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(OnlineProduct.name.ilike(like), OnlineProduct.name_si.ilike(like),
                              OnlineProduct.sku.ilike(like)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(OnlineProduct.name).limit(limit)
                             .offset(offset))).all()
    return {"total": total, "items": [{
        "online_product_id": str(p.online_product_id), "name": p.name, "name_si": p.name_si,
        "image_url": p.image_url, "sourcing_group": g,
        "mode": "partner" if g in EXTERNAL else "own"} for p, g in rows]}


async def share(db: AsyncSession, branch_id: uuid.UUID, scope: set[uuid.UUID] | None) -> dict:
    """How much of the branch's list still comes from outside (owner's goal: 0 %)."""
    check_scope(scope, branch_id)
    base = select(func.count()).select_from(OnlineBranchListing).where(
        OnlineBranchListing.branch_id == branch_id)
    listed = await db.scalar(base) or 0
    partner = await db.scalar(base.where(OnlineBranchListing.sourcing_group.in_(EXTERNAL))) or 0
    pct = round(partner * 100 / listed, 1) if listed else 0.0
    link = await link_of_branch(db, branch_id)
    return {"listed": listed, "partner": partner, "own": listed - partner, "partner_pct": pct,
            "partner_id": str(link.supplier_id) if link else None}


async def price_changes(db: AsyncSession, scope: set[uuid.UUID] | None, *,
                        branch_id: uuid.UUID | None = None, unseen_only: bool = True,
                        limit: int = 100) -> list[dict]:
    stmt = (select(PartnerPriceChange, OnlineProduct, OnlineVariant, Supplier.name)
            .join(PartnerItem, PartnerItem.item_id == PartnerPriceChange.item_id)
            .join(OnlineProduct, OnlineProduct.online_product_id == PartnerItem.online_product_id)
            .join(OnlineVariant, OnlineVariant.online_variant_id == PartnerItem.online_variant_id)
            .join(Supplier, Supplier.supplier_id == PartnerItem.supplier_id))
    if branch_id is not None:
        check_scope(scope, branch_id)
        stmt = stmt.where(PartnerPriceChange.branch_id == branch_id)
    elif scope is not None:
        stmt = stmt.where(PartnerPriceChange.branch_id.in_(scope))
    if unseen_only:
        stmt = stmt.where(PartnerPriceChange.seen_at.is_(None))
    rows = (await db.execute(stmt.order_by(PartnerPriceChange.change_id.desc())
                             .limit(limit))).all()
    return [{"change_id": c.change_id, "branch_id": str(c.branch_id),
             "partner_name": pname, "online_product_id": str(p.online_product_id),
             "name": p.name, "variant": v.name or None,
             "old_price": money(c.old_price), "new_price": money(c.new_price),
             "rise": c.old_price is not None and c.new_price > c.old_price,
             "changed_at": c.changed_at, "seen_at": c.seen_at} for c, p, v, pname in rows]


async def mark_seen(db: AsyncSession, actor: Actor, change_id: int,
                    scope: set[uuid.UUID] | None) -> None:
    change = await db.get(PartnerPriceChange, change_id)
    if change is None or (scope is not None and change.branch_id not in scope):
        raise NotFound("price change not found")
    if change.seen_at is None:
        change.seen_at, change.seen_by = _now(), actor.user_id
        await db.flush()


# ======================================================================= partner app
async def my_links(db: AsyncSession, supplier_id: uuid.UUID) -> dict:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or supplier.kind != PARTNER:
        raise Forbidden("this is for fulfilment partners")
    return await partner_branches(db, supplier_id)


def _fillable(links: dict):
    """Listings this partner fills: Other Products at its branches, plus Our Store products
    where the link fills shortfalls."""
    group = OnlineBranchListing.sourcing_group
    conds = []
    for bid, link in links.items():
        groups = list(EXTERNAL) + (["store"] if link.fills_store_shortfall else [])
        conds.append(and_(OnlineBranchListing.branch_id == bid, group.in_(groups)))
    return or_(*conds)


async def my_items(db: AsyncSession, supplier_id: uuid.UUID, *, q: str | None = None,
                   branch_id: uuid.UUID | None = None, limit: int = 50,
                   offset: int = 0) -> dict:
    """Our product list, in our names, that this partner fills: one row per pack size with
    the partner's price to us and stock (F4)."""
    links = await my_links(db, supplier_id)
    if branch_id is not None:
        if branch_id not in links:
            raise Forbidden("you do not serve this branch")
        links = {branch_id: links[branch_id]}
    if not links:
        return {"total": 0, "items": []}
    stmt = (select(OnlineBranchListing.branch_id, OnlineBranchListing.sourcing_group,
                   Branch.name, OnlineProduct, OnlineVariant, PartnerItem)
            .join(Branch, Branch.branch_id == OnlineBranchListing.branch_id)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == OnlineBranchListing.online_product_id)
            .join(OnlineVariant, OnlineVariant.online_product_id
                  == OnlineProduct.online_product_id)
            .outerjoin(PartnerItem, and_(
                PartnerItem.supplier_id == supplier_id,
                PartnerItem.branch_id == OnlineBranchListing.branch_id,
                PartnerItem.online_variant_id == OnlineVariant.online_variant_id))
            .where(_fillable(links), OnlineProduct.is_active.is_(True),
                   OnlineVariant.is_active.is_(True)))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(OnlineProduct.name.ilike(like), OnlineProduct.name_si.ilike(like),
                              OnlineProduct.sku.ilike(like), OnlineVariant.sku.ilike(like)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(OnlineProduct.name, OnlineVariant.name)
                             .limit(limit).offset(offset))).all()
    return {"total": total, "items": [{
        "branch_id": str(bid), "branch_name": bname, "sourcing_group": group,
        "backup_only": group == "store",
        "online_product_id": str(p.online_product_id),
        "online_variant_id": str(v.online_variant_id),
        "name": p.name, "name_si": p.name_si, "variant": v.name or None,
        "display_size": p.display_size, "display_size_unit": p.display_size_unit,
        "image_url": p.image_url, "sku": v.sku or p.sku,
        "price_to_us": money(i.price_to_us) if i else None,
        "stock_qty": i.stock_qty if i else 0,
        "updated_at": i.updated_at if i else None}
        for bid, group, bname, p, v, i in rows]}


async def update_item(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                      *, branch_id: uuid.UUID, online_variant_id: uuid.UUID,
                      price_to_us=UNSET, stock_qty=UNSET,
                      ip_address: str | None = None) -> dict:
    """The partner sets its price to us and/or its stock for one pack size, only for a
    product it fills at that branch. A price change applies at once and becomes a staff
    alert (F10)."""
    links = await my_links(db, supplier_id)
    link = links.get(branch_id)
    if link is None:
        raise Forbidden("you do not serve this branch")
    variant = await db.get(OnlineVariant, online_variant_id)
    if variant is None or not variant.is_active:
        raise NotFound("item not found")
    listing = await db.get(OnlineBranchListing, (branch_id, variant.online_product_id))
    if listing is None or fills(listing.sourcing_group, link) is None:
        raise Forbidden("this branch does not take this product from you")
    if price_to_us is not UNSET and (price_to_us is None or price_to_us <= 0):
        raise SupplyError("the price must be above 0")
    if stock_qty is not UNSET and (stock_qty is None or stock_qty < 0):
        raise SupplyError("the stock cannot be below 0")
    item = await db.scalar(select(PartnerItem).where(
        PartnerItem.supplier_id == supplier_id, PartnerItem.branch_id == branch_id,
        PartnerItem.online_variant_id == online_variant_id).with_for_update())
    if item is None:
        item = PartnerItem(supplier_id=supplier_id, branch_id=branch_id,
                           online_product_id=variant.online_product_id,
                           online_variant_id=online_variant_id, stock_qty=0)
        db.add(item)
        await db.flush()
    before = {"price_to_us": money(item.price_to_us), "stock_qty": item.stock_qty}
    if price_to_us is not UNSET and price_to_us != item.price_to_us:
        db.add(PartnerPriceChange(item_id=item.item_id, branch_id=branch_id,
                                  old_price=item.price_to_us, new_price=price_to_us,
                                  changed_by=supplier_user_id))
        item.price_to_us = Decimal(price_to_us)
    if stock_qty is not UNSET:
        item.stock_qty = int(stock_qty)
    item.updated_by = supplier_user_id
    after = {"price_to_us": money(item.price_to_us), "stock_qty": item.stock_qty}
    if after != before:
        await audit.record(db, supplier_user_id=supplier_user_id, action="partner.item",
                           entity="partner_item", entity_id=item.item_id, before=before,
                           after=after, ip_address=ip_address)
    await db.flush()
    return {"online_variant_id": str(online_variant_id), "branch_id": str(branch_id), **after}
