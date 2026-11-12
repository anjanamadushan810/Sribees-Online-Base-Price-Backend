"""Supplier orders (PLAN.md decisions 7, 10, 11, 15-17) and base prices (decision 5).

- Staff build a draft for one supplier and one branch; lines are whole boxes of a
  supplier offer that has a confirmed Online match (QUESTIONS.md #10) and a known
  units-per-box. Prices are frozen on the line.
- Branch managers may only raise drafts (requests) for their own branches; purchasing or
  the owner submits. Approval: ORDER_APPROVAL=local -> the owner approves here (the
  owner's own submit counts as approved, audited); =finance -> Finance answers later.
- The supplier sees the order from 'approved' on, and accepts (optionally stating its
  delivery charge) or declines. Accepting writes the base price of every line.
- Cancelling voids the base prices it wrote.
Every step writes an outbox event (Finance PO, supplier notification).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.permissions import SELF_APPROVING_ROLES, has_permission
from app.models import (
    BasePrice,
    Branch,
    OfferMatch,
    OutboxEvent,
    Supplier,
    SupplierOffer,
    SupplierProduct,
    SupplyOrder,
    SupplyOrderLine,
)
from app.services import audit
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

SUPPLIER_VISIBLE = ("approved", "accepted", "declined", "cancelled", "received")


def _now() -> datetime:
    return datetime.now(UTC)


def _emit(db: AsyncSession, event: str, order: SupplyOrder, **extra) -> None:
    db.add(OutboxEvent(event=event, payload={
        "order_id": str(order.order_id), "order_no": order.order_no,
        "supplier_id": str(order.supplier_id), "branch_id": str(order.branch_id),
        "status": order.status, **extra}))


def order_totals(order: SupplyOrder) -> dict:
    goods = sum((ln.line_total for ln in order.lines), Decimal(0))
    return {"goods_total": goods, "delivery_charge": order.delivery_charge,
            "total": goods + (order.delivery_charge or Decimal(0))}


MAX_LINE_TOTAL = Decimal("100000000000")  # Rs 100 billion: fits numeric(14,2) with room


async def _stale_lines(db: AsyncSession, order: SupplyOrder) -> list[str]:
    """Lines whose offer, item, price, units or confirmed match changed since they were
    added (review 2026-11-12 #1: a corrected match must never carry an old line's base
    price onto the wrong Online product)."""
    stale = []
    for ln in order.lines:
        offer = await db.get(SupplierOffer, ln.offer_id, with_for_update=True)
        product = await db.get(SupplierProduct, offer.product_id) if offer else None
        match = await db.scalar(select(OfferMatch).where(
            OfferMatch.offer_id == ln.offer_id, OfferMatch.status == "confirmed"))
        if (offer is None or product is None or not offer.is_active or not product.is_active
                or offer.price != ln.unit_price or offer.units_per_box != ln.units_per_box
                or match is None or match.online_product_id != ln.online_product_id
                or match.online_variant_id != ln.online_variant_id):
            stale.append(f"{ln.heading} {ln.label}".strip())
    return stale


async def _refuse_stale(db: AsyncSession, order: SupplyOrder) -> None:
    stale = await _stale_lines(db, order)
    if stale:
        raise Conflict("these lines changed since they were added (price, units, match or "
                       "no longer offered); remove and add them again: " + ", ".join(stale))


# ---------------------------------------------------------------- staff side
class StaffActor:
    """The parts of a signed-in staff user the rules need."""

    def __init__(self, user_id: uuid.UUID, role: str, branch_scope: set | None):
        self.user_id, self.role, self.branch_scope = user_id, role, branch_scope

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)

    def sees_branch(self, branch_id: uuid.UUID) -> bool:
        return self.branch_scope is None or branch_id in self.branch_scope


async def get(db: AsyncSession, order_id: uuid.UUID, actor: StaffActor, *,
              lock: bool = False) -> SupplyOrder:
    q = select(SupplyOrder).where(SupplyOrder.order_id == order_id)
    if lock:
        q = q.with_for_update()
    order = await db.scalar(q)
    if order is None or not actor.sees_branch(order.branch_id):
        raise NotFound("order not found")
    if lock:
        await db.refresh(order, ["lines"])
    return order


async def list_orders(db: AsyncSession, actor: StaffActor, *, supplier_id=None,
                      status=None) -> list[SupplyOrder]:
    q = select(SupplyOrder).order_by(SupplyOrder.created_at.desc()).limit(200)
    if supplier_id:
        q = q.where(SupplyOrder.supplier_id == supplier_id)
    if status:
        q = q.where(SupplyOrder.status == status)
    if actor.branch_scope is not None:
        q = q.where(SupplyOrder.branch_id.in_(actor.branch_scope or {uuid.uuid4()}))
    return list((await db.scalars(q)).all())


def _can_edit(order: SupplyOrder, actor: StaffActor) -> None:
    if order.status != "draft":
        raise Conflict("only a draft order can be changed")
    if actor.can("orders.create"):
        return
    # A branch manager changes only the requests they raised (review #8).
    if not (actor.can("orders.request") and order.created_by == actor.user_id):
        raise Forbidden("you do not have access to this")


async def create(db: AsyncSession, actor: StaffActor, *, supplier_id: uuid.UUID,
                 branch_id: uuid.UUID, note: str | None) -> SupplyOrder:
    if not (actor.can("orders.create") or actor.can("orders.request")):
        raise Forbidden("you do not have access to this")
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise NotFound("supplier not found")
    branch = await db.get(Branch, branch_id)
    if branch is None or not branch.is_active:
        raise NotFound("branch not found")
    if not actor.sees_branch(branch_id):
        raise Forbidden("you can order only for your own branch")
    order = SupplyOrder(supplier_id=supplier_id, branch_id=branch_id, note=note,
                        created_by=actor.user_id)
    db.add(order)
    await db.flush()
    await db.refresh(order, ["order_no", "lines"])
    await audit.record(db, user_id=actor.user_id, action="order.create", entity="order",
                       entity_id=order.order_id,
                       after={"supplier_id": str(supplier_id), "branch_id": str(branch_id)})
    return order


async def add_line(db: AsyncSession, actor: StaffActor, order_id: uuid.UUID, *,
                   offer_id: uuid.UUID, boxes: int) -> SupplyOrder:
    order = await get(db, order_id, actor, lock=True)
    _can_edit(order, actor)
    if boxes <= 0:
        raise SupplyError("order at least one box")
    offer = await db.get(SupplierOffer, offer_id)
    if offer is None or offer.supplier_id != order.supplier_id:
        raise NotFound("offer not found for this supplier")
    product = await db.get(SupplierProduct, offer.product_id)
    if not offer.is_active or not product.is_active:
        raise Conflict("the supplier no longer offers this")
    if offer.units_per_box is None:
        raise Conflict("units per box is not known for this offer; ask the supplier first")
    match = await db.scalar(select(OfferMatch).where(OfferMatch.offer_id == offer_id,
                                                     OfferMatch.status == "confirmed"))
    if match is None:
        raise Conflict("match this offer to an Online product before ordering it")
    if any(ln.offer_id == offer_id for ln in order.lines):
        raise Conflict("this offer is already on the order; change its boxes instead")
    if boxes * offer.units_per_box * offer.price > MAX_LINE_TOTAL:
        raise SupplyError("this line is too large")
    db.add(SupplyOrderLine(
        order_id=order.order_id, supplier_id=order.supplier_id, offer_id=offer_id,
        online_product_id=match.online_product_id, online_variant_id=match.online_variant_id,
        heading=product.heading, label=offer.label, boxes=boxes,
        units_per_box=offer.units_per_box, unit_price=offer.price, mrp=offer.mrp))
    await db.flush()
    await db.refresh(order, ["lines"])
    return order


async def set_boxes(db: AsyncSession, actor: StaffActor, order_id: uuid.UUID,
                    line_id: uuid.UUID, *, boxes: int) -> SupplyOrder:
    order = await get(db, order_id, actor, lock=True)
    _can_edit(order, actor)
    line = next((ln for ln in order.lines if ln.line_id == line_id), None)
    if line is None:
        raise NotFound("line not found")
    if boxes <= 0:
        await db.delete(line)
    else:
        if boxes * line.units_per_box * line.unit_price > MAX_LINE_TOTAL:
            raise SupplyError("this line is too large")
        line.boxes = boxes
    await db.flush()
    await db.refresh(order, ["lines"])
    for ln in order.lines:
        await db.refresh(ln, ["units", "line_total"])
    return order


async def submit(db: AsyncSession, actor: StaffActor, order_id: uuid.UUID) -> SupplyOrder:
    if not actor.can("orders.create"):
        raise Forbidden("purchasing or the owner submits orders")
    order = await get(db, order_id, actor, lock=True)
    if order.status != "draft":
        raise Conflict("only a draft order can be submitted")
    if not order.lines:
        raise SupplyError("add at least one line first")
    await _refuse_stale(db, order)
    order.status, order.submitted_by, order.submitted_at = "submitted", actor.user_id, _now()
    totals = order_totals(order)
    _emit(db, "order.submitted", order, goods_total=str(totals["goods_total"]),
          lines=[{"online_product_id": str(ln.online_product_id),
                  "online_variant_id": str(ln.online_variant_id)
                  if ln.online_variant_id else None,
                  "units": ln.boxes * ln.units_per_box, "unit_price": str(ln.unit_price)}
                 for ln in order.lines])
    await audit.record(db, user_id=actor.user_id, action="order.submit", entity="order",
                       entity_id=order.order_id, after={"goods_total": str(totals["goods_total"])})
    if get_settings().order_approval == "local" and actor.role in SELF_APPROVING_ROLES:
        await _approve(db, order, actor.user_id, self_approved=True)
    await db.flush()
    return order


async def _approve(db, order: SupplyOrder, user_id, *, self_approved: bool = False) -> None:
    order.status, order.approved_by, order.approved_at = "approved", user_id, _now()
    _emit(db, "order.approved", order)
    _emit(db, "supplier.notify", order, kind="new_order")
    await audit.record(db, user_id=user_id, action="order.approve", entity="order",
                       entity_id=order.order_id, after={"self_approved": self_approved})


async def approve(db: AsyncSession, actor: StaffActor, order_id: uuid.UUID) -> SupplyOrder:
    if get_settings().order_approval != "local":
        raise Conflict("orders are approved in Finance")
    if not actor.can("orders.approve"):
        raise Forbidden("only the owner approves orders")
    order = await get(db, order_id, actor, lock=True)
    if order.status != "submitted":
        raise Conflict("only a submitted order can be approved")
    if order.submitted_by == actor.user_id and actor.role not in SELF_APPROVING_ROLES:
        raise Forbidden("you cannot approve your own order")
    await _refuse_stale(db, order)
    await _approve(db, order, actor.user_id)
    await db.flush()
    return order


async def cancel(db: AsyncSession, actor: StaffActor, order_id: uuid.UUID, *,
                 reason: str) -> SupplyOrder:
    order = await get(db, order_id, actor, lock=True)
    if order.status not in ("draft", "submitted", "approved", "accepted"):
        raise Conflict(f"a {order.status} order cannot be cancelled")
    if order.status != "draft" and not actor.can("orders.create"):
        raise Forbidden("purchasing or the owner cancels a submitted order")
    if order.status == "draft" and not (actor.can("orders.create")
                                        or order.created_by == actor.user_id):
        raise Forbidden("you do not have access to this")
    was_visible = order.status in SUPPLIER_VISIBLE
    order.status, order.cancel_reason = "cancelled", reason.strip()
    order.cancelled_at, order.cancelled_by = _now(), actor.user_id
    voided = await db.execute(update(BasePrice).where(
        BasePrice.order_id == order.order_id, BasePrice.voided_at.is_(None))
        .values(voided_at=_now()))
    _emit(db, "order.cancelled", order, reason=order.cancel_reason)
    if was_visible:
        _emit(db, "supplier.notify", order, kind="order_cancelled")
    await audit.record(db, user_id=actor.user_id, action="order.cancel", entity="order",
                       entity_id=order.order_id,
                       after={"reason": order.cancel_reason,
                              "base_prices_voided": voided.rowcount})
    await db.flush()
    return order


# ---------------------------------------------------------------- supplier side
async def supplier_orders(db: AsyncSession, supplier_id: uuid.UUID) -> list[SupplyOrder]:
    return list((await db.scalars(
        select(SupplyOrder).where(SupplyOrder.supplier_id == supplier_id,
                                  SupplyOrder.status.in_(SUPPLIER_VISIBLE),
                                  SupplyOrder.approved_at.is_not(None))
        .order_by(SupplyOrder.approved_at.desc()).limit(200))).all())


async def supplier_order(db: AsyncSession, supplier_id: uuid.UUID, order_id: uuid.UUID, *,
                         lock: bool = False, mark_seen: bool = True) -> SupplyOrder:
    q = select(SupplyOrder).where(SupplyOrder.order_id == order_id,
                                  SupplyOrder.supplier_id == supplier_id)
    if lock:
        q = q.with_for_update()
    order = await db.scalar(q)
    if (order is None or order.status not in SUPPLIER_VISIBLE
            or order.approved_at is None):
        raise NotFound("order not found")
    if mark_seen and order.supplier_seen_at is None:
        order.supplier_seen_at = _now()
        await db.flush()
    return order


async def answer(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                 order_id: uuid.UUID, *, accept: bool, delivery_charge: Decimal | None,
                 note: str | None) -> SupplyOrder:
    order = await supplier_order(db, supplier_id, order_id, lock=True)
    if order.status != "approved":
        raise Conflict("this order has already been answered" if order.status in
                       ("accepted", "declined") else f"this order is {order.status}")
    await db.refresh(order, ["lines"])
    now = _now()
    order.status = "accepted" if accept else "declined"
    order.answered_at, order.answered_by = now, supplier_user_id
    order.supplier_note = (note or "").strip() or None
    skipped: list[str] = []
    if accept:
        if delivery_charge is not None:
            goods = order_totals(order)["goods_total"]
            cap = (goods * get_settings().delivery_charge_max_pct / 100).quantize(Decimal("0.01"))
            if delivery_charge > cap:
                raise SupplyError(f"delivery charge can be at most Rs {cap:,.2f} for this "
                                  "order; call Sribees for more")
            order.delivery_charge = delivery_charge
        for ln in order.lines:
            # The supplier agreed to the line as approved; but if staff re-matched the offer
            # since, the line's product is no longer trusted for a base price (review #1).
            match = await db.scalar(select(OfferMatch).where(
                OfferMatch.offer_id == ln.offer_id, OfferMatch.status == "confirmed"))
            if (match is None or match.online_product_id != ln.online_product_id
                    or match.online_variant_id != ln.online_variant_id):
                skipped.append(str(ln.line_id))
                continue
            db.add(BasePrice(online_product_id=ln.online_product_id,
                             online_variant_id=ln.online_variant_id, supplier_id=supplier_id,
                             order_id=order.order_id, line_id=ln.line_id,
                             unit_price=ln.unit_price, mrp=ln.mrp, recorded_at=now))
    _emit(db, f"order.{order.status}", order,
          delivery_charge=str(order.delivery_charge) if order.delivery_charge is not None
          else None, base_price_skipped_lines=skipped)
    await audit.record(db, supplier_user_id=supplier_user_id, action=f"order.{order.status}",
                       entity="order", entity_id=order.order_id,
                       after={"delivery_charge": str(delivery_charge)
                              if delivery_charge is not None else None})
    await db.flush()
    return order


# ---------------------------------------------------------------- base prices
async def current_base_prices(db: AsyncSession) -> list[BasePrice]:
    """Newest non-void base price per (Online product, variant)."""
    rows = (await db.scalars(
        select(BasePrice).where(BasePrice.voided_at.is_(None))
        .order_by(BasePrice.online_product_id, BasePrice.online_variant_id,
                  BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc())
        .distinct(BasePrice.online_product_id, BasePrice.online_variant_id))).all()
    return list(rows)


async def base_price_history(db: AsyncSession, online_product_id: uuid.UUID) -> list[BasePrice]:
    return list((await db.scalars(
        select(BasePrice).where(BasePrice.online_product_id == online_product_id)
        .order_by(BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc()))).all())


async def offers_for_product(db: AsyncSession, online_product_id: uuid.UUID) -> list[dict]:
    """Every active supplier offer confirmed as this Online product, cheapest first:
    what we could pay today, next to what we last paid."""
    rows = (await db.execute(
        select(SupplierOffer, SupplierProduct.heading, Supplier.name, OfferMatch)
        .join(OfferMatch, (OfferMatch.offer_id == SupplierOffer.offer_id)
              & (OfferMatch.status == "confirmed"))
        .join(SupplierProduct, SupplierProduct.product_id == SupplierOffer.product_id)
        .join(Supplier, Supplier.supplier_id == SupplierOffer.supplier_id)
        .where(OfferMatch.online_product_id == online_product_id,
               SupplierOffer.is_active.is_(True), SupplierProduct.is_active.is_(True),
               Supplier.is_active.is_(True))
        .order_by(SupplierOffer.price))).all()
    return [{"offer_id": o.offer_id, "supplier_id": o.supplier_id, "supplier_name": name,
             "heading": heading, "label": o.label, "price": o.price, "mrp": o.mrp,
             "units_per_box": o.units_per_box,
             "online_variant_id": m.online_variant_id} for o, heading, name, m in rows]
