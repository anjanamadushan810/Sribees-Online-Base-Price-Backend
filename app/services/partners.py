"""Fulfilment partner, phase P1 (docs/plans/fulfilment-partner-2026-11-27.md).

A fulfilment partner is a shop that fills, order by order, the products a branch lists but
does not carry. This module holds who the partner is and where it serves, which products a
branch takes from it (``branch_sourcing``), and the partner's own figures per pack size:
its price to us and its stock count (``partner_items``).

Guardrails (plan G1-G7): nothing here changes an existing flow. No sourcing row = own = as
before. Nothing names a branch or a partner: Nimali/Matugama are data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Branch,
    BranchSourcing,
    OnlineBranchListing,
    OnlineProduct,
    OnlineVariant,
    PartnerItem,
    PartnerPriceChange,
    StockBatch,
    Supplier,
    SupplierBranch,
)
from app.services import audit
from app.services.actors import Actor, _now
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

PARTNER = "fulfilment_partner"
WHOLESALE = "wholesale"
KINDS = (WHOLESALE, PARTNER)
UNSET = object()


def _money(v: Decimal | None) -> str | None:
    """Money leaves as a string with two decimals (never a float)."""
    return None if v is None else str(Decimal(v).quantize(Decimal("0.01")))


def _check_scope(scope: set[uuid.UUID] | None, branch_id: uuid.UUID) -> None:
    if scope is not None and branch_id not in scope:
        raise Forbidden("you do not have access to this branch")


async def _supplier(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFound("supplier not found")
    return supplier


async def _partner(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await _supplier(db, supplier_id)
    if supplier.kind != PARTNER:
        raise SupplyError("this supplier is not a fulfilment partner")
    if not supplier.is_active:
        raise SupplyError("this partner is switched off")
    return supplier


async def _branch(db: AsyncSession, branch_id: uuid.UUID) -> Branch:
    branch = await db.get(Branch, branch_id)
    if branch is None or not branch.is_active:
        raise NotFound("branch not found")
    return branch


async def partner_branches(db: AsyncSession, supplier_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.scalars(select(SupplierBranch.branch_id)
                            .where(SupplierBranch.supplier_id == supplier_id))
    return set(rows.all())


# ======================================================================= staff: set-up
async def set_kind(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID, kind: str) -> dict:
    if kind not in KINDS:
        raise SupplyError("kind must be wholesale or fulfilment_partner")
    supplier = await _supplier(db, supplier_id)
    if supplier.kind == kind:
        return {"supplier_id": str(supplier_id), "kind": kind}
    if kind == WHOLESALE and await db.scalar(
            select(SupplierBranch.branch_id).where(SupplierBranch.supplier_id == supplier_id)
            .limit(1)):
        raise Conflict("unlink the partner from its branches first")
    before = supplier.kind
    supplier.kind = kind
    await audit.record(db, user_id=actor.user_id, action="partner.kind", entity="supplier",
                       entity_id=supplier_id, before={"kind": before}, after={"kind": kind},
                       ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "kind": kind}


async def link_branch(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID,
                      branch_id: uuid.UUID, scope: set[uuid.UUID] | None) -> dict:
    _check_scope(scope, branch_id)
    await _partner(db, supplier_id)
    await _branch(db, branch_id)
    await db.execute(insert(SupplierBranch)
                     .values(supplier_id=supplier_id, branch_id=branch_id,
                             created_by=actor.user_id)
                     .on_conflict_do_nothing())
    await audit.record(db, user_id=actor.user_id, action="partner.link_branch",
                       entity="supplier", entity_id=supplier_id,
                       after={"branch_id": str(branch_id)}, ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "branch_id": str(branch_id)}


async def unlink_branch(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID,
                        branch_id: uuid.UUID, scope: set[uuid.UUID] | None) -> dict:
    """Clean exit (G6): the partner's products at this branch go back to own (rows removed;
    no row = own). Its figures stay for history; nothing reads them without a sourcing row."""
    _check_scope(scope, branch_id)
    await _supplier(db, supplier_id)
    gone = await db.execute(delete(BranchSourcing).where(
        BranchSourcing.branch_id == branch_id, BranchSourcing.partner_id == supplier_id))
    link = await db.execute(delete(SupplierBranch).where(
        SupplierBranch.supplier_id == supplier_id, SupplierBranch.branch_id == branch_id))
    if not link.rowcount:
        raise NotFound("this partner does not serve this branch")
    await audit.record(db, user_id=actor.user_id, action="partner.unlink_branch",
                       entity="supplier", entity_id=supplier_id,
                       after={"branch_id": str(branch_id), "products_back_to_own": gone.rowcount},
                       ip_address=actor.ip_address)
    await db.flush()
    return {"supplier_id": str(supplier_id), "branch_id": str(branch_id),
            "products_back_to_own": gone.rowcount}


async def set_sourcing(db: AsyncSession, actor: Actor, *, branch_id: uuid.UUID,
                       online_product_id: uuid.UUID, mode: str,
                       partner_id: uuid.UUID | None, scope: set[uuid.UUID] | None) -> dict:
    _check_scope(scope, branch_id)
    await _branch(db, branch_id)
    if await db.get(OnlineProduct, online_product_id) is None:
        raise NotFound("product not found; sync the Online catalogue")
    if mode == "own":
        partner_id = None
    elif mode == "partner":
        if partner_id is None:
            raise SupplyError("choose the partner")
        await _partner(db, partner_id)
        if branch_id not in await partner_branches(db, partner_id):
            raise SupplyError("this partner does not serve this branch")
    else:
        raise SupplyError("mode must be own or partner")
    row = await db.get(BranchSourcing, (branch_id, online_product_id))
    before = {"mode": row.mode, "partner_id": str(row.partner_id) if row.partner_id else None} \
        if row else {"mode": "own", "partner_id": None}
    if row is None:
        db.add(BranchSourcing(branch_id=branch_id, online_product_id=online_product_id,
                              mode=mode, partner_id=partner_id, changed_by=actor.user_id))
    else:
        row.mode, row.partner_id, row.changed_by = mode, partner_id, actor.user_id
    after = {"mode": mode, "partner_id": str(partner_id) if partner_id else None}
    await audit.record(db, user_id=actor.user_id, action="partner.sourcing",
                       entity="branch_sourcing", entity_id=online_product_id,
                       before=before, after={**after, "branch_id": str(branch_id)},
                       ip_address=actor.ip_address)
    await db.flush()
    return {"branch_id": str(branch_id), "online_product_id": str(online_product_id), **after}


def _carried(branch_id):
    """The branch has ever held stock of any pack size of the product (it carries it)."""
    return exists().where(
        StockBatch.branch_id == branch_id,
        StockBatch.product_id == OnlineVariant.online_variant_id,
        OnlineVariant.online_product_id == OnlineBranchListing.online_product_id)


async def seed(db: AsyncSession, actor: Actor, supplier_id: uuid.UUID, branch_id: uuid.UUID,
               scope: set[uuid.UUID] | None) -> dict:
    """F3: every active product the branch lists, has no sourcing decision yet and never held
    stock of, goes to this partner. Safe to run again (decided products are left alone)."""
    _check_scope(scope, branch_id)
    await _partner(db, supplier_id)
    if branch_id not in await partner_branches(db, supplier_id):
        raise SupplyError("this partner does not serve this branch")
    listed = await db.scalar(select(func.count()).select_from(OnlineBranchListing)
                             .where(OnlineBranchListing.branch_id == branch_id))
    if not listed:
        raise SupplyError("Online has not sent this branch's product list yet; sync the "
                          "Online catalogue first")
    decided = exists().where(BranchSourcing.branch_id == branch_id,
                             BranchSourcing.online_product_id
                             == OnlineBranchListing.online_product_id)
    base = (select(OnlineBranchListing.online_product_id)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == OnlineBranchListing.online_product_id)
            .where(OnlineBranchListing.branch_id == branch_id, OnlineProduct.is_active,
                   ~decided))
    carried = await db.scalar(select(func.count()).select_from(
        base.where(_carried(branch_id)).subquery()))
    pids = (await db.scalars(base.where(~_carried(branch_id)))).all()
    for pid in pids:
        db.add(BranchSourcing(branch_id=branch_id, online_product_id=pid, mode="partner",
                              partner_id=supplier_id, changed_by=actor.user_id))
    await audit.record(db, user_id=actor.user_id, action="partner.seed", entity="supplier",
                       entity_id=supplier_id,
                       after={"branch_id": str(branch_id), "added": len(pids),
                              "carried": carried}, ip_address=actor.ip_address)
    await db.flush()
    return {"added": len(pids), "skipped_carried": carried, "listed": listed}


# ======================================================================= staff: reading
async def sourcing_list(db: AsyncSession, branch_id: uuid.UUID, scope: set[uuid.UUID] | None,
                        *, mode: str | None = None, q: str | None = None,
                        limit: int = 50, offset: int = 0) -> dict:
    """The branch's listed products with where each comes from (no row = own)."""
    _check_scope(scope, branch_id)
    eff_mode = func.coalesce(BranchSourcing.mode, "own")
    stmt = (select(OnlineProduct, BranchSourcing, Supplier.name)
            .select_from(OnlineBranchListing)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == OnlineBranchListing.online_product_id)
            .outerjoin(BranchSourcing, and_(
                BranchSourcing.branch_id == branch_id,
                BranchSourcing.online_product_id == OnlineBranchListing.online_product_id))
            .outerjoin(Supplier, Supplier.supplier_id == BranchSourcing.partner_id)
            .where(OnlineBranchListing.branch_id == branch_id))
    if mode in ("own", "partner"):
        stmt = stmt.where(eff_mode == mode)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(OnlineProduct.name.ilike(like), OnlineProduct.name_si.ilike(like),
                              OnlineProduct.sku.ilike(like)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(OnlineProduct.name).limit(limit)
                             .offset(offset))).all()
    return {"total": total, "items": [{
        "online_product_id": str(p.online_product_id), "name": p.name, "name_si": p.name_si,
        "image_url": p.image_url, "mode": s.mode if s else "own",
        "partner_id": str(s.partner_id) if s and s.partner_id else None,
        "partner_name": pname} for p, s, pname in rows]}


async def share(db: AsyncSession, branch_id: uuid.UUID, scope: set[uuid.UUID] | None) -> dict:
    """How much of the branch's list a partner still fills (owner's goal: down to 0 %)."""
    _check_scope(scope, branch_id)
    listed = await db.scalar(select(func.count()).select_from(OnlineBranchListing)
                             .where(OnlineBranchListing.branch_id == branch_id)) or 0
    partner = await db.scalar(select(func.count()).select_from(BranchSourcing).where(
        BranchSourcing.branch_id == branch_id, BranchSourcing.mode == "partner")) or 0
    pct = round(partner * 100 / listed, 1) if listed else 0.0
    return {"listed": listed, "partner": partner, "own": max(listed - partner, 0),
            "partner_pct": pct}


async def price_changes(db: AsyncSession, scope: set[uuid.UUID] | None, *,
                        branch_id: uuid.UUID | None = None, unseen_only: bool = True,
                        limit: int = 100) -> list[dict]:
    stmt = (select(PartnerPriceChange, PartnerItem, OnlineProduct, OnlineVariant, Supplier.name)
            .join(PartnerItem, PartnerItem.item_id == PartnerPriceChange.item_id)
            .join(OnlineProduct, OnlineProduct.online_product_id == PartnerItem.online_product_id)
            .join(OnlineVariant, OnlineVariant.online_variant_id == PartnerItem.online_variant_id)
            .join(Supplier, Supplier.supplier_id == PartnerItem.supplier_id))
    if branch_id is not None:
        _check_scope(scope, branch_id)
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
             "old_price": _money(c.old_price), "new_price": _money(c.new_price),
             "rise": c.old_price is not None and c.new_price > c.old_price,
             "changed_at": c.changed_at, "seen_at": c.seen_at} for c, _i, p, v, pname in rows]


async def mark_seen(db: AsyncSession, actor: Actor, change_id: int,
                    scope: set[uuid.UUID] | None) -> None:
    change = await db.get(PartnerPriceChange, change_id)
    if change is None or (scope is not None and change.branch_id not in scope):
        raise NotFound("price change not found")
    if change.seen_at is None:
        change.seen_at, change.seen_by = _now(), actor.user_id
        await db.flush()


# ======================================================================= partner app
async def _my_branches(db: AsyncSession, supplier_id: uuid.UUID) -> set[uuid.UUID]:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or supplier.kind != PARTNER:
        raise Forbidden("this is for fulfilment partners")
    return await partner_branches(db, supplier_id)


def _variant_active():
    """Pack sizes a customer can buy: real ones, or the product's default when it has none
    (the default pack size carries the product's own id and is active only then)."""
    return OnlineVariant.is_active.is_(True)


async def my_items(db: AsyncSession, supplier_id: uuid.UUID, *, q: str | None = None,
                   branch_id: uuid.UUID | None = None, limit: int = 50,
                   offset: int = 0) -> dict:
    """Our product list, in our names, that this partner fills: one row per pack size with
    the partner's price to us and stock (F4)."""
    branches = await _my_branches(db, supplier_id)
    if branch_id is not None:
        if branch_id not in branches:
            raise Forbidden("you do not serve this branch")
        branches = {branch_id}
    if not branches:
        return {"total": 0, "items": []}
    stmt = (select(BranchSourcing.branch_id, Branch.name, OnlineProduct, OnlineVariant,
                   PartnerItem)
            .join(Branch, Branch.branch_id == BranchSourcing.branch_id)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == BranchSourcing.online_product_id)
            .join(OnlineVariant, OnlineVariant.online_product_id
                  == OnlineProduct.online_product_id)
            .outerjoin(PartnerItem, and_(
                PartnerItem.supplier_id == supplier_id,
                PartnerItem.branch_id == BranchSourcing.branch_id,
                PartnerItem.online_variant_id == OnlineVariant.online_variant_id))
            .where(BranchSourcing.mode == "partner", BranchSourcing.partner_id == supplier_id,
                   BranchSourcing.branch_id.in_(branches), OnlineProduct.is_active,
                   _variant_active()))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(OnlineProduct.name.ilike(like), OnlineProduct.name_si.ilike(like),
                              OnlineProduct.sku.ilike(like), OnlineVariant.sku.ilike(like)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(OnlineProduct.name, OnlineVariant.name)
                             .limit(limit).offset(offset))).all()
    return {"total": total, "items": [{
        "branch_id": str(bid), "branch_name": bname,
        "online_product_id": str(p.online_product_id),
        "online_variant_id": str(v.online_variant_id),
        "name": p.name, "name_si": p.name_si, "variant": v.name or None,
        "display_size": p.display_size, "display_size_unit": p.display_size_unit,
        "image_url": p.image_url, "sku": v.sku or p.sku,
        "price_to_us": _money(i.price_to_us) if i else None,
        "stock_qty": i.stock_qty if i else 0,
        "updated_at": i.updated_at if i else None} for bid, bname, p, v, i in rows]}


async def update_item(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                      *, branch_id: uuid.UUID, online_variant_id: uuid.UUID,
                      price_to_us=UNSET, stock_qty=UNSET,
                      ip_address: str | None = None) -> dict:
    """The partner sets its price to us and/or its stock for one pack size. Only for a
    product this branch takes from this partner. A price change applies at once and becomes
    a staff alert (F10)."""
    branches = await _my_branches(db, supplier_id)
    if branch_id not in branches:
        raise Forbidden("you do not serve this branch")
    variant = await db.get(OnlineVariant, online_variant_id)
    if variant is None or not variant.is_active:
        raise NotFound("item not found")
    src = await db.get(BranchSourcing, (branch_id, variant.online_product_id))
    if src is None or src.mode != "partner" or src.partner_id != supplier_id:
        raise Forbidden("this branch does not take this product from you")
    if price_to_us is not UNSET and price_to_us is not None and price_to_us <= 0:
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
    before = {"price_to_us": str(item.price_to_us) if item.price_to_us is not None else None,
              "stock_qty": item.stock_qty}
    if price_to_us is not UNSET and price_to_us != item.price_to_us:
        if price_to_us is None:
            raise SupplyError("the price cannot be cleared")
        db.add(PartnerPriceChange(item_id=item.item_id, branch_id=branch_id,
                                  old_price=item.price_to_us, new_price=price_to_us,
                                  changed_by=supplier_user_id))
        item.price_to_us = Decimal(price_to_us)
    if stock_qty is not UNSET:
        item.stock_qty = int(stock_qty)
    item.updated_by = supplier_user_id
    after = {"price_to_us": str(item.price_to_us) if item.price_to_us is not None else None,
             "stock_qty": item.stock_qty}
    if after != before:
        await audit.record(db, supplier_user_id=supplier_user_id, action="partner.item",
                           entity="partner_item", entity_id=item.item_id, before=before,
                           after=after, ip_address=ip_address)
    await db.flush()
    return {"online_variant_id": str(online_variant_id), "branch_id": str(branch_id),
            "price_to_us": _money(item.price_to_us), "stock_qty": item.stock_qty}
