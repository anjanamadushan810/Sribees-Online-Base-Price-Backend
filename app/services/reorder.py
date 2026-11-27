"""Automatic draft orders (owner MCQ 2026-11-26).

The branch manager sets a minimum (Online's low-stock threshold) and a maximum per product
in Online admin. Every morning at 07:00 (Sri Lanka) and on a button, for each branch:

    available = stock - reserved                      (Online's figures)
    below     = available < minimum
    need      = maximum - available - units on open orders (drafts included)

A product below its minimum with a need above zero goes on a DRAFT order, in units (not
boxes), for its supplier: the one staff chose for it, else the one we last bought it from.
The price is that supplier's last bill price. One automatic draft per supplier and branch;
a run only ADDS items that are on no open order, so what staff changed on a draft stays as
they left it. Nothing is sent: staff check the draft and submit it.

Left out, and listed for staff instead: no maximum, never bought (no supplier), kept by
weight, several pack sizes and none bought before, an item no longer sold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    BasePrice,
    Branch,
    MainSupplier,
    OnlineProduct,
    OnlineVariant,
    Receipt,
    ReceiptLine,
    ReorderRun,
    Supplier,
    SupplyOrder,
    SupplyOrderLine,
    SyncState,
    User,
)
from app.services import audit, orders
from app.services.errors import NotFound, SupplyError

OPEN = ("draft", "submitted", "approved", "accepted")
DAILY_KEY = "reorder_daily"


@dataclass
class Level:
    product_id: uuid.UUID
    stock_mode: str
    available: int
    minimum: int
    maximum: int | None
    visible: bool
    product_active: bool


@dataclass
class Plan:
    """What one run would do (also what it reports)."""

    lines: dict[uuid.UUID, list[dict]] = field(default_factory=dict)  # supplier -> lines
    left_out: list[dict] = field(default_factory=list)
    below: int = 0


def parse_levels(payload: dict) -> list[Level]:
    out = []
    for it in payload.get("items") or []:
        mx = it.get("max_stock_level")
        out.append(Level(
            product_id=uuid.UUID(it["product_id"]), stock_mode=it.get("stock_mode") or "unit",
            available=int(it.get("stock_quantity") or 0) - int(it.get("reserved_quantity") or 0),
            minimum=int(it.get("low_stock_threshold") or 0),
            maximum=int(mx) if mx is not None else None,
            visible=bool(it.get("visible")), product_active=bool(it.get("product_active", True))))
    return out


def units_needed(level: Level, on_order: int) -> int:
    """How many units to order; 0 = none. Never past the maximum."""
    if level.maximum is None or level.available >= level.minimum:
        return 0
    return max(level.maximum - max(level.available, 0) - on_order, 0)


async def fetch_levels(client: httpx.AsyncClient, online_branch_id: uuid.UUID) -> list[Level]:
    s = get_settings()
    if not s.online_api_url or not s.online_service_token:
        raise SupplyError("the link to SRIBEESonline is not set up yet")
    try:
        r = await client.get(f"{s.online_api_url.rstrip('/')}/internal/supply/stock-levels",
                             params={"branch_id": str(online_branch_id)}, timeout=60,
                             headers={"Authorization": f"Bearer {s.online_service_token}"})
    except httpx.HTTPError as exc:
        raise SupplyError(f"SRIBEESonline cannot be reached ({type(exc).__name__})") from None
    if r.status_code != 200:
        raise SupplyError(f"SRIBEESonline answered {r.status_code} for the stock levels")
    return parse_levels(r.json())


async def _pack_size(db: AsyncSession, product_id: uuid.UUID) -> uuid.UUID | None:
    """The pack size to order: the only one sold, else the one we bought last."""
    sizes = list(await db.scalars(select(OnlineVariant.online_variant_id).where(
        OnlineVariant.online_product_id == product_id, OnlineVariant.is_active.is_(True))))
    if len(sizes) == 1:
        return sizes[0]
    if not sizes:
        return None
    return await db.scalar(select(BasePrice.online_variant_id).where(
        BasePrice.online_product_id == product_id, BasePrice.voided_at.is_(None),
        BasePrice.online_variant_id.in_(sizes))
        .order_by(BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc()).limit(1))


async def _supplier_and_price(db: AsyncSession, variant_id: uuid.UUID
                              ) -> tuple[uuid.UUID | None, Decimal | None]:
    chosen = await db.get(MainSupplier, variant_id)
    bill = func.coalesce(BasePrice.bill_price, BasePrice.unit_price)
    q = (select(BasePrice.supplier_id, bill).where(
        BasePrice.online_variant_id == variant_id, BasePrice.voided_at.is_(None))
        .order_by(BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc()).limit(1))
    if chosen is not None:
        row = (await db.execute(q.where(BasePrice.supplier_id == chosen.supplier_id))).first()
        if row is None:  # chosen but never bought from them: the last price anyone billed
            last = (await db.execute(q)).first()
            return chosen.supplier_id, last[1] if last else None
        return row[0], row[1]
    row = (await db.execute(q)).first()
    return (row[0], row[1]) if row else (None, None)


async def on_order(db: AsyncSession, branch_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Units still to come per pack size on the branch's open orders (drafts included)."""
    got = (select(ReceiptLine.order_line_id, func.sum(ReceiptLine.units).label("units"))
           .join(Receipt, Receipt.receipt_id == ReceiptLine.receipt_id)
           .group_by(ReceiptLine.order_line_id).subquery())
    rows = await db.execute(
        select(SupplyOrderLine.online_variant_id,
               func.sum(SupplyOrderLine.units - func.coalesce(got.c.units, 0)))
        .join(SupplyOrder, SupplyOrder.order_id == SupplyOrderLine.order_id)
        .outerjoin(got, got.c.order_line_id == SupplyOrderLine.line_id)
        .where(SupplyOrder.branch_id == branch_id, SupplyOrder.status.in_(OPEN),
               SupplyOrderLine.online_variant_id.is_not(None))
        .group_by(SupplyOrderLine.online_variant_id))
    return {v: max(int(n or 0), 0) for v, n in rows}


async def plan(db: AsyncSession, branch_id: uuid.UUID, levels: list[Level]) -> Plan:
    out = Plan()
    coming = await on_order(db, branch_id)
    for lv in levels:
        if lv.available >= lv.minimum:
            continue
        out.below += 1
        product = await db.get(OnlineProduct, lv.product_id)
        name = product.name if product else str(lv.product_id)

        def leave(reason: str, lv: Level = lv, name: str = name, **extra) -> None:
            out.left_out.append({"online_product_id": str(lv.product_id), "name": name,
                                 "reason": reason, "available": lv.available,
                                 "minimum": lv.minimum, "maximum": lv.maximum, **extra})

        if product is None or not product.is_active or not lv.product_active:
            leave("not_sold")
            continue
        if lv.maximum is None:
            leave("no_maximum")
            continue
        if lv.stock_mode == "loose":
            leave("by_weight")
            continue
        variant_id = await _pack_size(db, lv.product_id)
        if variant_id is None:
            leave("pack_size")
            continue
        need = units_needed(lv, coming.get(variant_id, 0))
        if need <= 0:
            continue  # enough is already on its way
        supplier_id, price = await _supplier_and_price(db, variant_id)
        if supplier_id is None:
            leave("no_supplier", online_variant_id=str(variant_id), need=need)
            continue
        supplier = await db.get(Supplier, supplier_id)
        if supplier is None or not supplier.is_active:
            leave("supplier_inactive", online_variant_id=str(variant_id), need=need)
            continue
        out.lines.setdefault(supplier_id, []).append({
            "online_variant_id": variant_id, "name": name, "units": need,
            "unit_price": price if price is not None else Decimal("0"),
            "available": lv.available, "maximum": lv.maximum})
    return out


async def _system_actor(db: AsyncSession, user_id: uuid.UUID | None) -> orders.StaffActor:
    """Who the drafts are made by: the user who pressed the button, else the first owner."""
    if user_id is None:
        user_id = await db.scalar(select(User.user_id).where(
            User.role == "owner", User.is_active.is_(True)).order_by(User.created_at).limit(1))
        if user_id is None:
            raise SupplyError("no owner account to make the drafts with")
    return orders.StaffActor(user_id, "owner", None)


async def apply(db: AsyncSession, branch: Branch, p: Plan, *,
                user_id: uuid.UUID | None) -> dict:
    actor = await _system_actor(db, user_id)
    drafts = []
    for supplier_id, lines in p.lines.items():
        order = await db.scalar(select(SupplyOrder).where(
            SupplyOrder.supplier_id == supplier_id, SupplyOrder.branch_id == branch.branch_id,
            SupplyOrder.status == "draft", SupplyOrder.is_auto_draft.is_(True))
            .order_by(SupplyOrder.created_at).limit(1))
        if order is None:
            order = await orders.create(db, actor, supplier_id=supplier_id,
                                        branch_id=branch.branch_id,
                                        note="Automatic order: stock below the minimum")
            order.is_auto_draft = True
        added = 0
        for ln in lines:
            if any(x.online_variant_id == ln["online_variant_id"] for x in order.lines):
                continue  # already on this draft: staff's number stands
            order = await orders.add_line(db, actor, order.order_id,
                                          online_variant_id=ln["online_variant_id"],
                                          units=ln["units"], unit_price=ln["unit_price"])
            added += 1
        drafts.append({"order_id": str(order.order_id), "order_no": order.order_no,
                       "supplier_id": str(supplier_id),
                       "supplier_name": (await db.get(Supplier, supplier_id)).name,
                       "added": added, "lines": len(order.lines)})
    return {"drafts": drafts, "left_out": p.left_out, "below_minimum": p.below,
            "added": sum(d["added"] for d in drafts)}


async def run_branch(db: AsyncSession, client: httpx.AsyncClient, branch: Branch, *,
                     user_id: uuid.UUID | None) -> dict:
    if branch.online_branch_id is None:
        raise SupplyError(f"branch {branch.name} has no SRIBEESonline branch id")
    levels = await fetch_levels(client, branch.online_branch_id)
    result = await apply(db, branch, await plan(db, branch.branch_id, levels), user_id=user_id)
    result["checked"] = len(levels)
    db.add(ReorderRun(branch_id=branch.branch_id, ran_by=user_id, result=result))
    await audit.record(db, user_id=user_id, action="reorder.run", entity="branch",
                       entity_id=branch.branch_id,
                       after={"added": result["added"], "drafts": len(result["drafts"]),
                              "left_out": len(result["left_out"])})
    await db.flush()
    return result


async def branches(db: AsyncSession, scope: set[uuid.UUID] | None) -> list[Branch]:
    q = select(Branch).where(Branch.is_active.is_(True), Branch.is_head_office.is_(False),
                             Branch.online_branch_id.is_not(None)).order_by(Branch.name)
    return [b for b in await db.scalars(q) if scope is None or b.branch_id in scope]


async def latest(db: AsyncSession, scope: set[uuid.UUID] | None) -> list[dict]:
    out = []
    for b in await branches(db, scope):
        run = await db.scalar(select(ReorderRun).where(ReorderRun.branch_id == b.branch_id)
                              .order_by(ReorderRun.ran_at.desc()).limit(1))
        out.append({"branch_id": str(b.branch_id), "branch_name": b.name,
                    "ran_at": run.ran_at if run else None,
                    "result": run.result if run else None})
    return out


async def set_main_supplier(db: AsyncSession, variant_id: uuid.UUID,
                            supplier_id: uuid.UUID | None, *, user_id: uuid.UUID) -> None:
    if await db.get(OnlineVariant, variant_id) is None:
        raise NotFound("this item is not an Online pack size")
    row = await db.get(MainSupplier, variant_id)
    before = str(row.supplier_id) if row else None
    if supplier_id is None:
        if row is not None:
            await db.delete(row)
    else:
        supplier = await db.get(Supplier, supplier_id)
        if supplier is None or not supplier.is_active:
            raise NotFound("supplier not found")
        if row is None:
            db.add(MainSupplier(online_variant_id=variant_id, supplier_id=supplier_id,
                                set_by=user_id))
        else:
            row.supplier_id, row.set_by = supplier_id, user_id
    await audit.record(db, user_id=user_id, action="reorder.main_supplier", entity="item",
                       entity_id=variant_id, before={"supplier_id": before},
                       after={"supplier_id": str(supplier_id) if supplier_id else None})
    await db.flush()


def due_today(now: datetime, last_day: str | None, hour: int) -> bool:
    """The daily run: once a day, from `hour` o'clock in Sri Lanka."""
    local = now.astimezone(orders.COLOMBO)
    return hour >= 0 and local.hour >= hour and last_day != local.date().isoformat()


async def daily(sessionmaker, client: httpx.AsyncClient, now: datetime) -> dict | None:
    """The worker's morning run (owner: 07:00 every day). None = not due."""
    hour = get_settings().reorder_daily_hour
    async with sessionmaker() as db, db.begin():
        state = await db.get(SyncState, DAILY_KEY, with_for_update=True)
        if not due_today(now, (state.value or {}).get("day") if state else None, hour):
            return None
        day = now.astimezone(orders.COLOMBO).date().isoformat()
        if state is None:
            db.add(SyncState(key=DAILY_KEY, value={"day": day}))
        else:
            state.value = {"day": day}
    done: dict = {}
    async with sessionmaker() as db:
        found = await branches(db, None)
    for b in found:
        try:
            async with sessionmaker() as db, db.begin():
                branch = await db.get(Branch, b.branch_id)
                r = await run_branch(db, client, branch, user_id=None)
                done[b.name] = r["added"]
        except SupplyError as exc:
            done[b.name] = f"failed: {exc}"
    return done
