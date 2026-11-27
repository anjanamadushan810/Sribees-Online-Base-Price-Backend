"""Fulfilment requests (plan P2): an Online order's lines the branch's partner fills.

Read from Online's order events (the same feed as the pack-scan holds, worker
app/services/online_events.py):

- An order of a branch with a partner, in a live status, gets ONE request (FR-000001) for
  its "Other Products" lines (Online sourcing_group external_*), and, where the link says so,
  for the part of an "Our Store" line the shelf cannot cover (F2).
- The first event of a brand-new order carries no lines (Online writes it before they
  exist): we ask Online for the order (GET /internal/supply/orders/{id}).
- While the partner has not marked it ready, a later event re-reads the lines (the order may
  have been edited). Cancelling the order cancels an open request.
- The partner's stock count goes down by what a request takes and back up on a cancel.

A branch without a partner is never touched (G3); a failure here never holds up the
pack-scan work in the same event (G7, the caller runs this in a savepoint).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Branch,
    FulfilmentLine,
    FulfilmentRequest,
    OnlineBranchListing,
    OutboxEvent,
    PartnerItem,
    Pick,
    StockBatch,
    SupplierBranch,
)
from app.services import partners
from app.services.actors import _now
from app.services.errors import Conflict, NotFound
from app.services.settings import next_doc_no

log = logging.getLogger("supply.partner")

# Online statuses in which an order is live and not packed yet
LIVE = ("pending", "confirmed", "processing", "packing")
OPEN = ("new", "seen", "ready")
EDITABLE = ("new", "seen")


# ---------------------------------------------------------------- Online order lookup
async def fetch_order(order_id: uuid.UUID) -> dict | None:
    """The order as Online shows it to the partner (lines + who it is for). None when the
    link is not set up or Online cannot answer now (a later event brings the lines)."""
    s = get_settings()
    if not s.online_api_url or not s.online_service_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"{s.online_api_url.rstrip('/')}/internal/supply/orders/{order_id}"
            r = await client.get(url, headers={"Authorization": f"Bearer {s.online_service_token}"})
    except httpx.HTTPError as exc:
        log.warning("Online order %s not read: %s", order_id, exc)
        return None
    if r.status_code != 200:
        log.warning("Online order %s answered %s", order_id, r.status_code)
        return None
    return r.json()


def _customer(fr: FulfilmentRequest, detail: dict | None) -> None:
    if not detail:
        return
    fr.customer_name = (detail.get("customer_name") or "")[:200] or None
    fr.customer_phone = (detail.get("phone") or "")[:30] or None
    fr.alternate_phone = (detail.get("alternate_phone") or "")[:30] or None
    fr.delivery_address = detail.get("address")
    fr.order_notes = detail.get("notes")


# ---------------------------------------------------------------- what the partner fills
async def own_free(db: AsyncSession, branch_id: uuid.UUID, variant_id: uuid.UUID) -> int:
    """Units of a pack size on the branch's shelf not held for another order yet."""
    on_hand = await db.scalar(select(func.coalesce(func.sum(StockBatch.qty_on_hand), 0)).where(
        StockBatch.branch_id == branch_id, StockBatch.product_id == variant_id,
        StockBatch.status == "available"))
    held = await db.scalar(select(func.coalesce(func.sum(Pick.qty), 0))
                           .join(StockBatch, StockBatch.batch_id == Pick.batch_id).where(
        StockBatch.branch_id == branch_id, Pick.product_id == variant_id,
        Pick.status == "picked"))
    return max(int(Decimal(on_hand) - Decimal(held)), 0)


async def wanted(db: AsyncSession, link: SupplierBranch, lines: list[dict]) -> list[dict]:
    """Per order line, how many units the partner fills (only lines with something)."""
    out = []
    for ln in lines:
        qty = int(ln.get("quantity") or 0)
        if qty <= 0 or not ln.get("order_item_id") or not ln.get("product_id"):
            continue
        product_id = uuid.UUID(str(ln["product_id"]))
        variant_id = uuid.UUID(str(ln["variant_id"])) if ln.get("variant_id") else product_id
        listing = await db.get(OnlineBranchListing, (link.branch_id, product_id))
        how = partners.fills(listing.sourcing_group if listing else None, link)
        if how == "all":
            need = qty
        elif how == "shortfall":
            need = max(qty - await own_free(db, link.branch_id, variant_id), 0)
        else:
            need = 0
        if need > 0:
            out.append({"order_item_id": uuid.UUID(str(ln["order_item_id"])),
                        "product_id": product_id, "variant_id": variant_id, "qty": need,
                        "name": (ln.get("name") or "")[:255] or "?",
                        "variant_name": (ln.get("variant_name") or "")[:120] or None})
    return out


async def _item(db, fr: FulfilmentRequest, variant_id: uuid.UUID) -> PartnerItem | None:
    return await db.scalar(select(PartnerItem).where(
        PartnerItem.supplier_id == fr.supplier_id, PartnerItem.branch_id == fr.branch_id,
        PartnerItem.online_variant_id == variant_id).with_for_update())


async def _take(db, fr: FulfilmentRequest, line: FulfilmentLine) -> None:
    """The partner's stock goes down by the line (never below 0; what it took is kept)."""
    item = await _item(db, fr, line.online_variant_id)
    if item is None:
        line.stock_taken = 0
        return
    line.price_to_us = item.price_to_us
    line.stock_taken = min(item.stock_qty, line.qty)
    item.stock_qty -= line.stock_taken


async def _give_back(db, fr: FulfilmentRequest, line: FulfilmentLine) -> None:
    if line.stock_taken:
        item = await _item(db, fr, line.online_variant_id)
        if item is not None:
            item.stock_qty += line.stock_taken
        line.stock_taken = 0


def _notify(db, fr: FulfilmentRequest, kind: str) -> None:
    label = fr.request_no + (f" · {fr.order_number}" if fr.order_number else "")
    db.add(OutboxEvent(event="supplier.notify", payload={
        "kind": kind, "supplier_id": str(fr.supplier_id), "branch_id": str(fr.branch_id),
        "order_id": str(fr.request_id), "order_no": label, "status": fr.status}))


async def _cancel(db, fr: FulfilmentRequest, reason: str) -> None:
    for line in fr.lines:
        await _give_back(db, fr, line)
    fr.status, fr.cancelled_at, fr.cancel_reason = "cancelled", _now(), reason[:200]
    _notify(db, fr, "partner_cancelled")


# ---------------------------------------------------------------- the event
async def apply_event(db: AsyncSession, ev: dict, *, fetch=fetch_order) -> str:
    p = ev.get("payload") or {}
    if ev.get("type") != "order.status" or not p.get("order_id"):
        return "ignored"
    online_branch = (p.get("branch") or {}).get("branch_id")
    if not online_branch:
        return "ignored"
    branch = await db.scalar(select(Branch).where(
        Branch.online_branch_id == uuid.UUID(str(online_branch))))
    if branch is None:
        return "ignored"
    order_id = uuid.UUID(str(p["order_id"]))
    fr = await db.scalar(select(FulfilmentRequest)
                         .where(FulfilmentRequest.online_order_id == order_id)
                         .with_for_update())
    to = p.get("to_status")
    if to == "cancelled":
        if fr is not None and fr.status in OPEN:
            await _cancel(db, fr, "order cancelled")
            await db.flush()
            return "partner:cancelled"
        return "ignored"
    if to not in LIVE or (fr is not None and fr.status not in EDITABLE):
        return "ignored"
    link = await partners.link_of_branch(db, branch.branch_id)
    if link is None:
        return "ignored"  # no partner here: exactly as before (G3)
    if fr is not None and link.supplier_id != fr.supplier_id:
        return "ignored"  # the partner changed after this request: it finishes as it is
    lines, detail = p.get("lines") or [], None
    if not lines:
        if fr is not None:
            return "ignored"
        detail = await fetch(order_id)
        if not detail:
            return "partner:waiting_for_lines"
        lines = detail.get("lines") or []
    need = await wanted(db, link, lines)
    if fr is None:
        if not need:
            return "ignored"
        fr = FulfilmentRequest(request_no=await next_doc_no(db, "FR"),
                               supplier_id=link.supplier_id, branch_id=branch.branch_id,
                               online_order_id=order_id, order_number=p.get("order_number"),
                               status="new", lines=[])
        _customer(fr, detail)
        db.add(fr)
        await db.flush()
        for n in need:
            line = FulfilmentLine(request_id=fr.request_id, order_item_id=n["order_item_id"],
                                  online_product_id=n["product_id"],
                                  online_variant_id=n["variant_id"], name=n["name"],
                                  variant_name=n["variant_name"], qty=n["qty"])
            await _take(db, fr, line)
            fr.lines.append(line)
        _notify(db, fr, "partner_request")
        await db.flush()
        return "partner:created"
    # an edited order while the partner has not packed yet: follow it
    by_item = {ln.order_item_id: ln for ln in fr.lines}
    changed = False
    for n in need:
        line = by_item.pop(n["order_item_id"], None)
        if line is None:
            line = FulfilmentLine(request_id=fr.request_id, order_item_id=n["order_item_id"],
                                  online_product_id=n["product_id"],
                                  online_variant_id=n["variant_id"], name=n["name"],
                                  variant_name=n["variant_name"], qty=n["qty"])
            await _take(db, fr, line)
            fr.lines.append(line)
            changed = True
        elif line.qty != n["qty"]:
            await _give_back(db, fr, line)
            line.qty = n["qty"]
            await _take(db, fr, line)
            changed = True
    for line in by_item.values():
        await _give_back(db, fr, line)
        fr.lines.remove(line)
        changed = True
    if not fr.lines:
        await _cancel(db, fr, "nothing left for the partner")
        await db.flush()
        return "partner:cancelled"
    if changed:
        _notify(db, fr, "partner_changed")
        await db.flush()
        return "partner:changed"
    return "ignored"


# ---------------------------------------------------------------- reading
def summary(fr: FulfilmentRequest, *, with_customer: bool) -> dict:
    out = {"request_id": str(fr.request_id), "request_no": fr.request_no,
           "online_order_id": str(fr.online_order_id), "order_number": fr.order_number,
           "branch_id": str(fr.branch_id), "supplier_id": str(fr.supplier_id),
           "status": fr.status, "created_at": fr.created_at, "seen_at": fr.seen_at,
           "ready_at": fr.ready_at, "handed_at": fr.handed_at, "cancelled_at": fr.cancelled_at,
           "cancel_reason": fr.cancel_reason,
           "units": sum(ln.qty for ln in fr.lines),
           "lines": [{"line_id": str(ln.line_id), "order_item_id": str(ln.order_item_id),
                      "online_product_id": str(ln.online_product_id),
                      "online_variant_id": str(ln.online_variant_id), "name": ln.name,
                      "variant": ln.variant_name, "qty": ln.qty,
                      "price_to_us": partners.money(ln.price_to_us),
                      "ticked_qty": ln.ticked_qty} for ln in fr.lines]}
    if with_customer:
        out.update(customer_name=fr.customer_name, customer_phone=fr.customer_phone,
                   alternate_phone=fr.alternate_phone, delivery_address=fr.delivery_address,
                   order_notes=fr.order_notes)
    return out


async def partner_list(db: AsyncSession, supplier_id: uuid.UUID, *, open_only: bool = True,
                       limit: int = 50, offset: int = 0) -> list[dict]:
    await partners.my_links(db, supplier_id)
    stmt = select(FulfilmentRequest).where(FulfilmentRequest.supplier_id == supplier_id)
    if open_only:
        stmt = stmt.where(FulfilmentRequest.status.in_(OPEN))
    rows = await db.scalars(stmt.order_by(FulfilmentRequest.created_at.desc())
                            .limit(limit).offset(offset))
    return [summary(fr, with_customer=True) for fr in rows]


async def _partner_request(db, supplier_id, request_id, *, lock=False) -> FulfilmentRequest:
    await partners.my_links(db, supplier_id)
    stmt = select(FulfilmentRequest).where(FulfilmentRequest.request_id == request_id)
    fr = await db.scalar(stmt.with_for_update() if lock else stmt)
    if fr is None or fr.supplier_id != supplier_id:
        raise NotFound("request not found")
    return fr


async def partner_open(db: AsyncSession, supplier_id: uuid.UUID, request_id: uuid.UUID,
                       *, fetch=fetch_order) -> dict:
    """The partner opens a request: marked seen; who it is for is filled in when missing."""
    fr = await _partner_request(db, supplier_id, request_id, lock=True)
    if fr.status == "new":
        fr.status, fr.seen_at = "seen", _now()
    if fr.customer_name is None and fr.status in OPEN:
        _customer(fr, await fetch(fr.online_order_id))
    await db.flush()
    return summary(fr, with_customer=True)


async def partner_ready(db: AsyncSession, supplier_id: uuid.UUID,
                        request_id: uuid.UUID) -> dict:
    """The partner has packed the bag: Matugama staff can collect / receive it."""
    fr = await _partner_request(db, supplier_id, request_id, lock=True)
    if fr.status == "ready":
        return summary(fr, with_customer=True)
    if fr.status not in EDITABLE:
        raise Conflict(f"this request is {fr.status}")
    fr.status, fr.ready_at = "ready", _now()
    fr.seen_at = fr.seen_at or fr.ready_at
    await db.flush()
    return summary(fr, with_customer=True)


async def staff_list(db: AsyncSession, scope: set[uuid.UUID] | None, *,
                     branch_id: uuid.UUID | None = None, status: str | None = None,
                     limit: int = 50, offset: int = 0) -> list[dict]:
    stmt = select(FulfilmentRequest)
    if branch_id is not None:
        partners.check_scope(scope, branch_id)
        stmt = stmt.where(FulfilmentRequest.branch_id == branch_id)
    elif scope is not None:
        stmt = stmt.where(FulfilmentRequest.branch_id.in_(scope))
    if status == "open":
        stmt = stmt.where(FulfilmentRequest.status.in_(OPEN))
    elif status:
        stmt = stmt.where(FulfilmentRequest.status == status)
    rows = await db.scalars(stmt.order_by(FulfilmentRequest.created_at.desc())
                            .limit(limit).offset(offset))
    return [summary(fr, with_customer=True) for fr in rows]


async def staff_get(db: AsyncSession, scope: set[uuid.UUID] | None, *,
                    request_id: uuid.UUID | None = None, order_number: str | None = None,
                    lock: bool = False) -> FulfilmentRequest:
    stmt = select(FulfilmentRequest)
    if request_id is not None:
        stmt = stmt.where(FulfilmentRequest.request_id == request_id)
    elif order_number:
        stmt = stmt.where(FulfilmentRequest.order_number == order_number.strip())
    else:
        raise NotFound("request not found")
    fr = await db.scalar(stmt.with_for_update() if lock else stmt)
    if fr is None or (scope is not None and fr.branch_id not in scope):
        raise NotFound("request not found")
    return fr

