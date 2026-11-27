"""Fulfilment partner, plan P4: the bag's handover and the money (owner MCQ 2026-11-28).

- **Handover (F8, F20).** Branch staff scan the bag's slip (order number or FR number) and
  tick each line. Only a complete bag is taken: every line ticked to its full quantity.
- **The day's order (F18: one Finance PO per day).** After the day ends (Sri Lanka time) the
  day's handed requests of a branch become ONE quick-buy order for the partner, through the
  tested purchase path: order -> submit (Finance PO + its approval rule) -> one receipt
  (batches at the partner's price to us, base prices, `receipt.posted`, GRN journal). The
  lines are received as held (`hold`): no QR labels and never added to Online's stock
  (Online already counted them as the partner's). The partner's price is the one on each
  request line (a mid-day price change gives the same pack size two receipt lines).
- **Straight out to the customer.** In the same transaction each request's units leave
  their batch to its Online order (dispatch move + cost-of-sales journal, like a pack scan's
  delivery), so nothing stays on our shelf.
- **Back to our shelf.** A customer order cancelled / sent back after the handover, or items
  refused or returned, put those units back into their batch (return move + journal). They
  then show on the held-stock list (no QR): staff label them and they go on sale as ours.
  Before the day's order exists, such units are simply not dispatched.

Money is high risk (plan): one journal per document (source_ref), every step idempotent.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    FulfilmentLine,
    FulfilmentRequest,
    PartnerItem,
    Product,
    ReceiptLine,
    StockBatch,
    Supplier,
    SyncState,
    User,
)
from app.services import audit, ledger, orders, receiving, stock
from app.services.actors import Actor, _now
from app.services.drafts import check_expiry
from app.services.errors import Conflict, NotFound, SupplyError
from app.services.partner_requests import summary

log = logging.getLogger("supply.partner_books")
COLOMBO = orders.COLOMBO
SOURCE = "sales"  # Finance's source for cost of goods sold (accounts 5000 / 1300)
STATE = "partner_books"
HANDOVER_FROM = ("new", "seen", "ready")


def local_day(at: datetime) -> date:
    return at.astimezone(COLOMBO).date()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=COLOMBO)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


# ======================================================================= handover
async def lookup(db: AsyncSession, scope: set[uuid.UUID] | None, code: str) -> dict:
    """The request a scanned slip names: its order number, or its FR number."""
    code = (code or "").strip()
    if not code:
        raise NotFound("request not found")
    stmt = select(FulfilmentRequest).where(
        (FulfilmentRequest.order_number == code) | (FulfilmentRequest.request_no == code))
    fr = await db.scalar(stmt.order_by(FulfilmentRequest.created_at.desc()).limit(1))
    if fr is None or (scope is not None and fr.branch_id not in scope):
        raise NotFound("request not found")
    return summary(fr, with_customer=True)


async def _staff_request(db, scope, request_id) -> FulfilmentRequest:
    fr = await db.scalar(select(FulfilmentRequest)
                         .where(FulfilmentRequest.request_id == request_id).with_for_update())
    if fr is None or (scope is not None and fr.branch_id not in scope):
        raise NotFound("request not found")
    return fr


def _apply_ticks(fr: FulfilmentRequest, ticks: list[tuple]) -> None:
    """ticks: (line_id, qty) or (line_id, qty, expiry_date or None)."""
    by_id = {ln.line_id: ln for ln in fr.lines}
    today = local_day(datetime.now(UTC))
    for tk in ticks:
        line_id, qty = tk[0], tk[1]
        expiry = tk[2] if len(tk) > 2 else None
        line = by_id.get(line_id)
        if line is None:
            raise NotFound("line not found on this request")
        if not (0 <= qty <= line.qty):
            raise SupplyError(f"{line.name}: tick between 0 and {line.qty}")
        if expiry is not None:
            check_expiry(expiry, today, None, line.name)
            line.expiry_date = expiry
        line.ticked_qty = qty


async def tick(db: AsyncSession, actor: Actor, scope: set[uuid.UUID] | None,
               request_id: uuid.UUID, ticks: list[tuple[uuid.UUID, int]]) -> dict:
    """Save what has arrived so far (the bag may come in parts; nothing is taken yet)."""
    fr = await _staff_request(db, scope, request_id)
    if fr.status not in HANDOVER_FROM:
        raise Conflict(f"this request is {fr.status}")
    _apply_ticks(fr, ticks)
    await db.flush()
    return summary(fr, with_customer=True)


async def handover(db: AsyncSession, actor: Actor, scope: set[uuid.UUID] | None,
                   request_id: uuid.UUID, ticks: list[tuple[uuid.UUID, int]]) -> dict:
    """Take the bag: only when every line has fully arrived (owner F20)."""
    fr = await _staff_request(db, scope, request_id)
    if fr.status == "handed":
        return summary(fr, with_customer=True)
    if fr.status not in HANDOVER_FROM:
        raise Conflict(f"this request is {fr.status}")
    _apply_ticks(fr, ticks)
    short = [f"{ln.name}: {ln.ticked_qty}/{ln.qty}" for ln in fr.lines if ln.ticked_qty < ln.qty]
    if short:
        raise Conflict("wait until the whole bag has come: " + "; ".join(short))
    fr.status, fr.handed_at, fr.handed_by = "handed", _now(), actor.user_id
    fr.ready_at = fr.ready_at or fr.handed_at
    await audit.record(db, user_id=actor.user_id, action="partner.handover",
                       entity="fulfilment_request", entity_id=fr.request_id,
                       after={"request_no": fr.request_no, "order_number": fr.order_number,
                              "units": sum(ln.qty for ln in fr.lines)},
                       ip_address=actor.ip_address)
    await db.flush()
    return summary(fr, with_customer=True)


# ======================================================================= the day's order
@dataclass
class _Part:
    variant_id: uuid.UUID
    price: Decimal
    expiry: date | None
    units: int
    lines: list[FulfilmentLine]


async def _price(db, fr: FulfilmentRequest, line: FulfilmentLine) -> Decimal | None:
    if line.price_to_us is not None:
        return line.price_to_us
    return await db.scalar(select(PartnerItem.price_to_us).where(
        PartnerItem.supplier_id == fr.supplier_id, PartnerItem.branch_id == fr.branch_id,
        PartnerItem.online_variant_id == line.online_variant_id))


async def _owner_actor(db: AsyncSession) -> tuple[orders.StaffActor, Actor]:
    user_id = await db.scalar(select(User.user_id).where(
        User.role == "owner", User.is_active.is_(True)).order_by(User.created_at).limit(1))
    if user_id is None:
        raise SupplyError("no owner account to write the partner's day order with")
    return orders.StaffActor(user_id, "owner", None), Actor(user_id=user_id, role="owner")


async def close_day(db: AsyncSession, branch_id: uuid.UUID, day: date) -> dict:
    """Book one branch's handed requests of one day (Sri Lanka). Idempotent: booked
    requests are never booked again; nothing to book = nothing written."""
    start, end = _day_bounds(day)
    frs = list(await db.scalars(
        select(FulfilmentRequest).where(
            FulfilmentRequest.branch_id == branch_id, FulfilmentRequest.status == "handed",
            FulfilmentRequest.booked_at.is_(None), FulfilmentRequest.handed_at >= start,
            FulfilmentRequest.handed_at < end)
        .order_by(FulfilmentRequest.handed_at).with_for_update()))
    if not frs:
        return {"booked": 0}
    by_partner: dict[uuid.UUID, list[FulfilmentRequest]] = defaultdict(list)
    for fr in frs:
        by_partner[fr.supplier_id].append(fr)
    staff, actor = await _owner_actor(db)
    out = {"booked": 0, "orders": []}
    for supplier_id, group in by_partner.items():
        order_no = await _book(db, staff, actor, supplier_id, branch_id, day, group)
        out["booked"] += len(group)
        out["orders"].append(order_no)
    return out


async def _book(db, staff, actor, supplier_id, branch_id, day, frs) -> str:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise SupplyError("the partner is switched off; switch it on to book its goods")
    parts: dict[tuple[uuid.UUID, Decimal, date | None], _Part] = {}
    missing = []
    for fr in frs:
        for line in fr.lines:
            price = await _price(db, fr, line)
            if price is None:
                missing.append(line.name)
                continue
            key = (line.online_variant_id, Decimal(price), line.expiry_date)
            part = parts.setdefault(key, _Part(line.online_variant_id, Decimal(price),
                                               line.expiry_date, 0, []))
            part.units += line.qty
            part.lines.append(line)
    if missing:
        raise SupplyError("the partner has no price for: " + ", ".join(sorted(set(missing))))
    for part in parts.values():
        product = await db.get(Product, part.variant_id)
        if product is not None and product.unit == "kg":
            raise SupplyError(f"{product.name} is kept by weight; book it by hand")

    order = await orders.create(db, staff, supplier_id=supplier_id, branch_id=branch_id,
                                note=f"Partner goods {day.isoformat()} "
                                     f"({len(frs)} customer orders)")
    order.is_quick_buy = True
    first_price: dict[uuid.UUID, Decimal] = {}
    units_of: dict[uuid.UUID, int] = defaultdict(int)
    for part in parts.values():
        first_price.setdefault(part.variant_id, part.price)
        units_of[part.variant_id] += part.units
    line_of: dict[uuid.UUID, uuid.UUID] = {}
    for variant_id, units in units_of.items():
        order = await orders.add_line(db, staff, order.order_id, online_variant_id=variant_id,
                                      units=units, unit_price=first_price[variant_id])
        line_of[variant_id] = next(x.line_id for x in order.lines
                                   if x.online_variant_id == variant_id)
    await db.refresh(order, ["lines"])
    await orders.submit(db, staff, order.order_id)  # Finance's PO + approval, as usual
    order.approved_by_name = order.approved_by_name or "Partner goods (handed over)"
    keys = list(parts)
    total = sum((p.price * p.units for p in parts.values()), Decimal("0"))
    receipt = await receiving.receive(
        db, actor, order.order_id, received_date=day,
        invoice_no=f"{supplier.code}-{day.isoformat()}"[:40], invoice_total=total,
        notes="Fulfilment partner: goods packed straight into customer orders",
        transport_charge=Decimal("0"), split_lines=True, expiry_optional=True,
        lines=[receiving.ReceiptLineIn(order_line_id=line_of[parts[k].variant_id],
                                       units=parts[k].units, product_id=parts[k].variant_id,
                                       unit_price=parts[k].price, hold=True,
                                       expiry_date=parts[k].expiry) for k in keys])
    rows = sorted(receipt.lines, key=lambda r: r.line_no)
    now = _now()
    for k, row in zip(keys, rows, strict=True):
        for line in parts[k].lines:
            line.batch_id = row.batch_id
    for fr in frs:
        fr.booked_at, fr.supply_order_id = now, order.order_id
        await _dispatch(db, fr, day)
    await audit.record(db, user_id=actor.user_id, action="partner.day_booked",
                       entity="order", entity_id=order.order_id,
                       after={"order_no": order.order_no, "day": day.isoformat(),
                              "requests": [fr.request_no for fr in frs], "total": str(total)})
    await db.flush()
    return order.order_no


async def _journal(db, fr: FulfilmentRequest, value: Decimal, *, back: bool, when: date,
                   ref: str):
    if value <= 0:
        return None
    dr, cr = ("1300", "5000") if back else ("5000", "1300")
    what = "partner goods back on our shelf" if back else "partner goods packed for the customer"
    label = fr.order_number or fr.request_no
    return await ledger.post_journal(
        db, entry_date=when, source=SOURCE, source_ref=ref,
        memo=f"Online order {label}: {what}",
        lines=[ledger.LineIn(dr, fr.branch_id, debit=value, memo=label),
               ledger.LineIn(cr, fr.branch_id, credit=value, memo=label)], user_id=None)


async def _dispatch(db, fr: FulfilmentRequest, when: date) -> None:
    """The request's units (less any already back) leave their batches to the order."""
    plan = []
    for line in fr.lines:
        qty = line.qty - line.returned_qty
        if qty > 0 and line.batch_id is not None:
            batch = await db.get(StockBatch, line.batch_id)
            plan.append((line, batch, Decimal(qty),
                         await stock.issue_value(db, batch, Decimal(qty))))
    value = sum((-v for *_x, v in plan), Decimal("0"))
    journal = await _journal(db, fr, value, back=False, when=when,
                             ref=f"partner:{fr.request_id}:out")
    for _line, batch, qty, v in plan:
        await stock.move(db, batch=batch, move_type="dispatch", qty=-qty, value=v,
                         move_date=when, doc_type="online_order", doc_id=fr.online_order_id,
                         user_id=None, journal_id=journal.journal_id if journal else None)


async def _give_back(db, fr: FulfilmentRequest, want: dict[uuid.UUID, int], when: date,
                     ref: str) -> int:
    """`want`: order_item_id -> units back. Before booking: only remembered (not
    dispatched). After: returned into the batch at the cost it left at."""
    plan = []
    for line in fr.lines:
        n = min(want.get(line.order_item_id, 0), line.qty - line.returned_qty)
        if n > 0:
            plan.append((line, n))
    if not plan:
        return 0
    if fr.booked_at is None:
        for line, n in plan:
            line.returned_qty += n
        return sum(n for _, n in plan)
    moves = []
    for line, n in plan:
        batch = await db.get(StockBatch, line.batch_id)
        received = await db.scalar(select(ReceiptLine).where(
            ReceiptLine.batch_id == line.batch_id))
        # a unit left at its batch's cost: the receipt's price (no transport on these)
        moves.append((line, n, batch, stock.money(received.unit_price * n)))
    journal = await _journal(db, fr, sum((v for *_x, v in moves), Decimal("0")), back=True,
                             when=when, ref=ref)
    for line, n, batch, v in moves:
        await stock.move(db, batch=batch, move_type="return", qty=Decimal(n), value=v,
                         move_date=when, doc_type="online_order", doc_id=fr.online_order_id,
                         user_id=None, journal_id=journal.journal_id if journal else None)
        line.returned_qty += n
    return sum(n for _, n in plan)


async def after_handover(db: AsyncSession, fr: FulfilmentRequest, ev: dict) -> str:
    """An Online order event for a request already handed over."""
    p = ev.get("payload") or {}
    key = ev.get("event_id") or ev.get("seq")
    today = datetime.now(COLOMBO).date()
    to = p.get("to_status")
    if ev.get("type") == "order.undelivered":
        items = p.get("undelivered") or []
        why = "undelivered"
    elif to in ("cancelled", "rto_initiated"):
        if fr.order_closed:
            return "ignored"
        fr.order_closed, fr.closed_at = ("cancelled" if to == "cancelled" else "rto"), _now()
        items = [{"order_item_id": str(ln.order_item_id), "quantity": ln.qty} for ln in fr.lines]
        why = to
    elif to == "refunded" and p.get("returned") and p.get("return_received_at"):
        items, why = p["returned"], "returned"
    else:
        return "ignored"
    want: dict[uuid.UUID, int] = defaultdict(int)
    for it in items:
        try:
            want[uuid.UUID(str(it["order_item_id"]))] += int(it.get("quantity") or 0)
        except (KeyError, ValueError):
            continue
    n = await _give_back(db, fr, want, today, ref=f"partner:{fr.request_id}:{why}:{key}")
    await db.flush()
    return f"partner:back:{n}" if n else "ignored"


# ======================================================================= worker
async def due(db: AsyncSession, today: date) -> list[tuple[uuid.UUID, date]]:
    """(branch, day) pairs with handed, unbooked requests from before today."""
    start, _ = _day_bounds(today)
    rows = (await db.execute(
        select(FulfilmentRequest.branch_id, FulfilmentRequest.handed_at).where(
            FulfilmentRequest.status == "handed", FulfilmentRequest.booked_at.is_(None),
            FulfilmentRequest.handed_at < start))).all()
    return sorted({(b, local_day(at)) for b, at in rows}, key=lambda x: (x[1], str(x[0])))


async def run_once(sessionmaker: async_sessionmaker, now: datetime | None = None) -> dict:
    """After midnight (Sri Lanka): each earlier day's handed requests become that day's
    partner order. A day that cannot be booked (a missing price) is kept for staff and
    tried again next round."""
    today = local_day(now or datetime.now(UTC))
    async with sessionmaker() as db:
        pairs = await due(db, today)
    done, problems = {}, {}
    for branch_id, day in pairs:
        try:
            async with sessionmaker() as db, db.begin():
                r = await close_day(db, branch_id, day)
                done[f"{branch_id}:{day}"] = r.get("orders")
        except SupplyError as exc:
            problems[f"{branch_id}:{day}"] = exc.message
            log.warning("partner day %s %s not booked: %s", branch_id, day, exc.message)
    async with sessionmaker() as db, db.begin():
        state = await db.get(SyncState, STATE, with_for_update=True)
        value = {"problems": problems, "at": datetime.now(UTC).isoformat()}
        if state is None:
            db.add(SyncState(key=STATE, value=value))
        else:
            state.value = value
    return {"booked": done, "problems": problems} if (done or problems) else {}


async def status(db: AsyncSession) -> dict:
    state = await db.get(SyncState, STATE)
    return dict(state.value) if state else {"problems": {}}

