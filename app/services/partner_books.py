"""Fulfilment partner, plan P4: the bag's handover, the money, and returns to the partner
(owner MCQ 2026-11-28; fresh review 2026-11-28 folded in).

- **Handover (F8, F20).** Branch staff scan the bag's slip (order number or FR number) and
  tick each line. Only a complete bag is taken: every line ticked to its full quantity, every
  line priced by the partner (owner: a price is mandatory), and an expiry typed for products
  that track one.
- **The day's order (F18: one Finance PO per day).** After the day ends (Sri Lanka time, with
  a 15-minute grace) the day's handed requests of a branch become ONE quick-buy order for the
  partner: one order line per pack size and price (so the PO equals the receipt), submit
  (Finance PO + its approval rule), one receipt (held: no QR labels, never added to Online's
  stock; base prices; `receipt.posted` -> Finance's invoice; GRN journal 1300/2020).
- **Straight out to the customer.** In the same transaction each request's units leave their
  batch to its Online order (dispatch move + cost of sales 5000/1300), so nothing stays on
  our shelf.
- **Back to the partner (owner 2026-11-28).** Units of a cancelled / RTO order, or refused or
  returned items, are NOT ours: the rider brings them to the branch, staff hand them to the
  partner ("sent"), the partner accepts them in the app. On acceptance the partner's stock
  count goes back up and, for units already booked, the goods come back from the customer
  (1300/5000) and go back to the partner (2020/1300, supplier_return), and Finance gets
  `supplier_return.posted` to make the supplier credit (2010/2020) that the owner's next
  payment uses. Units claimed back before the day was booked are simply never bought.

Money is high risk: one journal per document (source_ref), every step idempotent.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Branch,
    FulfilmentLine,
    FulfilmentRequest,
    OnlineProduct,
    OnlineVariant,
    OutboxEvent,
    PartnerItem,
    PartnerReturn,
    PartnerReturnLine,
    Product,
    Receipt,
    ReceiptLine,
    StockBatch,
    Supplier,
    SupplyOrder,
    SupplyOrderLine,
    SyncState,
    User,
)
from app.services import audit, ledger, orders, receiving, stock
from app.services.actors import Actor, _now
from app.services.drafts import check_expiry
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError
from app.services.partner_requests import summary
from app.services.settings import next_doc_no

log = logging.getLogger("supply.partner_books")
COLOMBO = orders.COLOMBO
SALES = "sales"  # Finance's source for cost of goods sold (accounts 5000 / 1300)
PURCHASING = receiving.SOURCE  # goods received / sent back (accounts 1300 / 2020)
STATE = "partner_books"
HANDOVER_FROM = ("new", "seen", "ready")
GRACE = time(0, 15)  # a handover committing just after midnight still lands in its day


def local_day(at: datetime) -> date:
    return at.astimezone(COLOMBO).date()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=COLOMBO)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _today() -> date:
    return local_day(datetime.now(UTC))


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
    today = _today()
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
               request_id: uuid.UUID, ticks: list[tuple]) -> dict:
    """Save what has arrived so far (the bag may come in parts; nothing is taken yet)."""
    fr = await _staff_request(db, scope, request_id)
    if fr.status not in HANDOVER_FROM:
        raise Conflict(f"this request is {fr.status}")
    _apply_ticks(fr, ticks)
    await db.flush()
    return summary(fr, with_customer=True)


async def _item_price(db, fr: FulfilmentRequest, variant_id: uuid.UUID) -> Decimal | None:
    return await db.scalar(select(PartnerItem.price_to_us).where(
        PartnerItem.supplier_id == fr.supplier_id, PartnerItem.branch_id == fr.branch_id,
        PartnerItem.online_variant_id == variant_id))


async def handover(db: AsyncSession, actor: Actor, scope: set[uuid.UUID] | None,
                   request_id: uuid.UUID, ticks: list[tuple]) -> dict:
    """Take the bag: complete, priced, and with an expiry where the product needs one."""
    fr = await _staff_request(db, scope, request_id)
    if fr.status == "handed":
        return summary(fr, with_customer=True)
    if fr.status not in HANDOVER_FROM:
        raise Conflict(f"this request is {fr.status}")
    _apply_ticks(fr, ticks)
    short = [f"{ln.name}: {ln.ticked_qty}/{ln.qty}" for ln in fr.lines if ln.ticked_qty < ln.qty]
    if short:
        raise Conflict("wait until the whole bag has come: " + "; ".join(short))
    problems = []
    for ln in fr.lines:
        if ln.price_to_us is None:  # owner: a price is mandatory
            ln.price_to_us = await _item_price(db, fr, ln.online_variant_id)
        if ln.price_to_us is None:
            problems.append(f"{ln.name}: the partner has not entered its price")
        product = await db.get(Product, ln.online_variant_id)
        if product is not None and product.track_expiry and ln.expiry_date is None:
            problems.append(f"{ln.name}: type the expiry date from the pack")
    if problems:
        raise Conflict("; ".join(problems))
    fr.status, fr.handed_at, fr.handed_by = "handed", _now(), actor.user_id
    fr.ready_at = fr.ready_at or fr.handed_at
    await audit.record(db, user_id=actor.user_id, action="partner.handover",
                       entity="fulfilment_request", entity_id=fr.request_id,
                       after={"request_no": fr.request_no, "order_number": fr.order_number,
                              "units": sum(ln.qty for ln in fr.lines)},
                       ip_address=actor.ip_address)
    await db.flush()
    return summary(fr, with_customer=True)


async def exclude(db: AsyncSession, actor: Actor, scope: set[uuid.UUID] | None,
                  request_id: uuid.UUID, reason: str) -> dict:
    """The owner takes a handed request out of the day order (it is booked by hand)."""
    if actor.role != "owner":
        raise Forbidden("only the owner takes a request out of the partner's day order")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise SupplyError("say why (at least 5 letters)")
    fr = await _staff_request(db, scope, request_id)
    if fr.status != "handed" or fr.booked_at is not None:
        raise Conflict("only a handed request not booked yet can be taken out")
    fr.booked_at, fr.excluded_reason = _now(), reason[:300]
    await audit.record(db, user_id=actor.user_id, action="partner.exclude",
                       entity="fulfilment_request", entity_id=fr.request_id,
                       after={"request_no": fr.request_no, "reason": reason[:300]},
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
    per_unit: Decimal = Decimal("1")  # stock measure per unit: kg for goods kept by weight
    by_weight: bool = False


async def _is_kg(db, variant_id: uuid.UUID) -> bool:
    """Our stock item is kept by weight: an existing kg item, or (none yet) a weight option
    of a product Online sells loose, so it is created by weight (review 2)."""
    product = await db.get(Product, variant_id)
    if product is not None:
        return product.unit == "kg"
    variant = await db.get(OnlineVariant, variant_id)
    online = await db.get(OnlineProduct, variant.online_product_id) if variant else None
    return bool(online and online.stock_mode == "loose" and variant.weight_grams)


async def _per_unit(db, variant_id: uuid.UUID) -> Decimal:
    """1 for pieces; kilograms per unit when our stock item is kept by weight (a loose
    product's weight option: its grams, else 1 kg)."""
    if not await _is_kg(db, variant_id):
        return Decimal("1")
    variant = await db.get(OnlineVariant, variant_id)
    grams = variant.weight_grams if variant and variant.weight_grams else 1000
    return (Decimal(grams) / 1000).quantize(Decimal("0.001"))


async def _owner_actor(db: AsyncSession) -> tuple[orders.StaffActor, Actor]:
    user_id = await db.scalar(select(User.user_id).where(
        User.role == "owner", User.is_active.is_(True)).order_by(User.created_at).limit(1))
    if user_id is None:
        raise SupplyError("no owner account to write the partner's day order with")
    return orders.StaffActor(user_id, "owner", None), Actor(user_id=user_id, role="owner")


async def _order_line(db, order: SupplyOrder, variant_id: uuid.UUID, units: int,
                      price: Decimal) -> SupplyOrderLine:
    """Like orders.add_line, but one pack size may have several prices (a mid-day price
    change): each price is its own line, so the PO equals the receipt (review #5)."""
    variant = await db.get(OnlineVariant, variant_id)
    product = await db.get(OnlineProduct, variant.online_product_id) if variant else None
    if variant is None or not variant.is_active or product is None or not product.is_active:
        raise SupplyError(f"{product.name if product else variant_id} is no longer sold on "
                          "SRIBEES Online; take its request out (owner) or switch it back on")
    if units * price > orders.MAX_LINE_TOTAL:
        raise SupplyError(f"{product.name}: this line is too large")
    label = (variant.name if variant.name and variant.name.lower() not in product.name.lower()
             else "")
    row = SupplyOrderLine(order_id=order.order_id, supplier_id=order.supplier_id, offer_id=None,
                          online_product_id=product.online_product_id,
                          online_variant_id=variant_id, heading=product.name[:200],
                          label=label[:120], boxes=units, units_per_box=1, unit_price=price,
                          mrp=None)
    db.add(row)
    await db.flush()
    return row


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
    out: dict = {"booked": 0, "orders": [], "problems": []}
    for supplier_id, group in by_partner.items():
        try:
            async with db.begin_nested():  # one partner's problem never blocks another's
                order_no = await _book(db, staff, actor, supplier_id, branch_id, day, group)
        except Exception as exc:  # noqa: BLE001 -- that partner waits; the others book
            out["problems"].append(str(getattr(exc, "message", exc))[:300])
            log.warning("partner %s day %s not booked: %s", supplier_id, day,
                        out["problems"][-1])
            continue
        out["booked"] += len(group)
        out["orders"].append(order_no)
    if out["problems"] and not out["booked"]:
        raise SupplyError("; ".join(out["problems"]))
    return out


async def _book(db, staff, actor, supplier_id, branch_id, day, frs) -> str:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or not supplier.is_active:
        raise SupplyError("the partner is switched off; switch it on to book its goods")
    parts: dict[tuple, _Part] = {}
    missing = []
    for fr in frs:
        for line in fr.lines:
            units = line.qty - line.returned_qty  # units claimed back are never bought
            if units <= 0:
                continue
            price = line.price_to_us or await _item_price(db, fr, line.online_variant_id)
            if price is None:
                missing.append(line.name)
                continue
            line.price_to_us = price
            key = (line.online_variant_id, Decimal(price), line.expiry_date)
            part = parts.setdefault(key, _Part(line.online_variant_id, Decimal(price),
                                               line.expiry_date, 0, []))
            part.units += units
            part.lines.append(line)
    if missing:
        raise SupplyError("the partner has no price for: " + ", ".join(sorted(set(missing))))
    now = _now()
    if not parts:  # everything came back before the day was booked: nothing to buy
        for fr in frs:
            fr.booked_at = now
        await db.flush()
        return "-"
    for part in parts.values():
        part.per_unit = await _per_unit(db, part.variant_id)
        part.by_weight = await _is_kg(db, part.variant_id)
        for line in part.lines:
            line.stock_per_unit = part.per_unit

    # One PO per day (F18). A pack size can sit on an order once (database rule), so a
    # mid-day price change puts its second price on a second order for the same day: each
    # PO then equals its receipt exactly (review #5). Almost every day has one tier.
    prices: dict[uuid.UUID, list[Decimal]] = defaultdict(list)
    for part in parts.values():
        if part.price not in prices[part.variant_id]:
            prices[part.variant_id].append(part.price)
    tiers: dict[int, list[tuple]] = defaultdict(list)
    for key, part in parts.items():
        tiers[prices[part.variant_id].index(part.price)].append(key)
    start, end = _day_bounds(day)
    earlier = await db.scalar(select(func.count(func.distinct(Receipt.receipt_id))).where(
        Receipt.receipt_id.in_(_partner_receipt_ids(supplier_id, branch_id)),
        Receipt.received_date == day))
    left: dict[uuid.UUID, list[Decimal]] = {}  # batch -> [qty, value] still on hand
    numbers = []
    for n, tier in enumerate(sorted(tiers), start=(earlier or 0) + 1):
        order = await _book_tier(db, staff, actor, supplier, branch_id, day,
                                 f"-{n}" if n > 1 else "", [(k, parts[k]) for k in tiers[tier]],
                                 len(frs), left)
        numbers.append(order)
    main = numbers[0]
    for fr in frs:
        fr.booked_at, fr.supply_order_id = now, main.order_id
        await _dispatch(db, fr, day, left)
    await audit.record(db, user_id=actor.user_id, action="partner.day_booked",
                       entity="order", entity_id=main.order_id,
                       after={"orders": [o.order_no for o in numbers], "day": day.isoformat(),
                              "requests": [fr.request_no for fr in frs]})
    await db.flush()
    return ", ".join(o.order_no for o in numbers)


def _partner_receipt_ids(supplier_id: uuid.UUID, branch_id: uuid.UUID):
    """Receipts of a partner's day orders: those holding its requests' batches."""
    batches = (select(FulfilmentLine.batch_id)
               .join(FulfilmentRequest, FulfilmentRequest.request_id == FulfilmentLine.request_id)
               .where(FulfilmentRequest.supplier_id == supplier_id,
                      FulfilmentRequest.branch_id == branch_id,
                      FulfilmentLine.batch_id.is_not(None)))
    return select(ReceiptLine.receipt_id).where(ReceiptLine.batch_id.in_(batches))


async def _book_tier(db, staff, actor, supplier, branch_id, day, suffix, parts, n_orders,
                     left) -> SupplyOrder:
    # one partner may serve several branches: the branch is in the invoice number, else
    # Finance finds the number taken and matches no invoice (review 2 HIGH)
    branch_code = (await db.get(Branch, branch_id)).code
    order = await orders.create(db, staff, supplier_id=supplier.supplier_id, branch_id=branch_id,
                                note=f"Partner goods {day.isoformat()}{suffix} "
                                     f"({n_orders} customer orders)")
    order.is_quick_buy = True
    line_of: dict[uuid.UUID, uuid.UUID] = {}
    units_of: dict[uuid.UUID, int] = defaultdict(int)
    price_of: dict[uuid.UUID, Decimal] = {}
    for _k, part in parts:
        units_of[part.variant_id] += part.units
        price_of[part.variant_id] = part.price
    for variant_id, units in units_of.items():
        row = await _order_line(db, order, variant_id, units, price_of[variant_id])
        line_of[variant_id] = row.line_id
    await db.refresh(order, ["lines"])
    await orders.submit(db, staff, order.order_id)  # Finance's PO + approval, as usual
    order.approved_by_name = order.approved_by_name or "Partner goods (handed over)"
    total = sum((p.price * p.units for _k, p in parts), Decimal("0"))
    receipt = await receiving.receive(
        db, actor, order.order_id, received_date=day,
        invoice_no=f"{supplier.code}-{branch_code}-{day.isoformat()}{suffix}",
        invoice_total=total,
        notes="Fulfilment partner: goods packed straight into customer orders",
        transport_charge=Decimal("0"), split_lines=True, pass_through=True,
        lines=[receiving.ReceiptLineIn(
            order_line_id=line_of[p.variant_id], units=p.units, product_id=p.variant_id,
            unit_price=p.price, hold=True, expiry_date=p.expiry,
            weight_kg=p.per_unit * p.units if p.by_weight else None) for _k, p in parts])
    rows = sorted(receipt.lines, key=lambda r: r.line_no)
    for (_k, part), row in zip(parts, rows, strict=True):
        batch = await db.get(StockBatch, row.batch_id)
        await db.refresh(batch, ["qty_on_hand", "value_on_hand"])
        left[row.batch_id] = [Decimal(batch.qty_on_hand), Decimal(batch.value_on_hand)]
        for line in part.lines:
            line.batch_id = row.batch_id
    return order


async def _journal(db, *, source: str, branch_id: uuid.UUID, dr: str, cr: str, value: Decimal,
                   when: date, ref: str, memo: str, label: str):
    if value <= 0:
        return None
    return await ledger.post_journal(
        db, entry_date=when, source=source, source_ref=ref, memo=memo,
        lines=[ledger.LineIn(dr, branch_id, debit=value, memo=label),
               ledger.LineIn(cr, branch_id, credit=value, memo=label)], user_id=None)


async def _dispatch(db, fr: FulfilmentRequest, when: date, left: dict) -> None:
    """The request's units (less any claimed back) leave their batches to the order. Values
    are taken in turn from what each batch still holds, so the last one empties it exactly."""
    plan = []
    for line in fr.lines:
        units = line.qty - line.returned_qty
        if units <= 0 or line.batch_id is None:
            continue
        qty = Decimal(units) * line.stock_per_unit  # pieces, or kilograms
        on_hand, value = left[line.batch_id]
        v = value if qty == on_hand else stock.money(value * qty / on_hand)
        left[line.batch_id] = [on_hand - qty, value - v]
        line.out_qty, line.out_value = units, v
        plan.append((line, qty, v))
    label = fr.order_number or fr.request_no
    journal = await _journal(db, source=SALES, branch_id=fr.branch_id, dr="5000", cr="1300",
                             value=sum((v for *_x, v in plan), Decimal("0")), when=when,
                             ref=f"partner:{fr.request_id}:out", label=label,
                             memo=f"Online order {label}: partner goods packed for the customer")
    for line, qty, v in plan:
        batch = await db.get(StockBatch, line.batch_id)
        await stock.move(db, batch=batch, move_type="dispatch", qty=-qty, value=-v,
                         move_date=when, doc_type="online_order", doc_id=fr.online_order_id,
                         user_id=None, journal_id=journal.journal_id if journal else None)


# ======================================================================= back to the partner
async def partner_units(db: AsyncSession, order_id: uuid.UUID, item_id: uuid.UUID) -> int:
    """Units of an Online order line the partner packed (a handed-over bag)."""
    got = await db.scalar(
        select(func.coalesce(func.sum(FulfilmentLine.qty), 0))
        .join(FulfilmentRequest, FulfilmentRequest.request_id == FulfilmentLine.request_id)
        .where(FulfilmentRequest.online_order_id == order_id,
               FulfilmentRequest.status == "handed", FulfilmentLine.order_item_id == item_id))
    return int(got or 0)


async def after_handover(db: AsyncSession, fr: FulfilmentRequest, ev: dict) -> str:
    """An Online order event for a request already handed over: what comes back goes back
    to the partner (owner 2026-11-28)."""
    p = ev.get("payload") or {}
    key = ev.get("event_id") or ev.get("seq")
    to = p.get("to_status")
    if ev.get("type") == "order.undelivered":
        items, why, whole = p.get("undelivered") or [], "undelivered", False
    elif to in ("cancelled", "rto_initiated"):
        if fr.order_closed:
            return "ignored"
        fr.order_closed, fr.closed_at = ("cancelled" if to == "cancelled" else "rto"), _now()
        items, why, whole = [], ("cancelled" if to == "cancelled" else "rto"), True
    elif to == "refunded" and p.get("returned") and p.get("return_received_at"):
        items, why, whole = p["returned"], "returned", False
    else:
        return "ignored"
    ref = f"{fr.request_id}:{why}:{key}"
    if await db.scalar(select(PartnerReturn.return_id).where(PartnerReturn.event_ref == ref)):
        return "ignored"
    want: dict[uuid.UUID, int] = defaultdict(int)
    if whole:
        for ln in fr.lines:
            want[ln.order_item_id] = ln.qty
    else:
        for it in items:
            try:
                want[uuid.UUID(str(it["order_item_id"]))] += int(it.get("quantity") or 0)
            except (KeyError, ValueError):
                continue
        # an order line split between our shelf and the partner (F2): refused units come
        # out of our own first. Only what the pack-scan part of THIS event took back is
        # subtracted (review #4, round 2 #3); a retried failure carries it in `_own_back`.
        own = dict(ev.get("_own_back") or {})
        own.update(db.info.get("own_back") or {})
        for item_id in list(want):
            want[item_id] = max(0, want[item_id] - int(own.get(f"{fr.online_order_id}:{item_id}",
                                                                0)))
    plan = []
    for ln in fr.lines:
        n = min(want.get(ln.order_item_id, 0), ln.qty - ln.returned_qty)
        if n > 0:
            plan.append((ln, n))
    if not plan:
        await db.flush()
        return "ignored"
    ret = PartnerReturn(return_no=await next_doc_no(db, "PRN"), request_id=fr.request_id,
                        supplier_id=fr.supplier_id, branch_id=fr.branch_id, reason=why,
                        status="waiting", event_ref=ref,
                        in_books=fr.booked_at is not None and fr.supply_order_id is not None,
                        lines=[])
    db.add(ret)
    for ln, n in plan:
        ln.returned_qty += n
        ret.lines.append(PartnerReturnLine(fr_line_id=ln.line_id, qty=n))
    await db.flush()
    db.add(OutboxEvent(event="supplier.notify", payload={
        "kind": "partner_return", "supplier_id": str(fr.supplier_id),
        "branch_id": str(fr.branch_id), "order_id": str(ret.return_id),
        "order_no": f"{ret.return_no} · {fr.order_number or fr.request_no}",
        "status": ret.status}))
    await db.flush()
    return f"partner:return:{sum(n for _, n in plan)}"


def return_view(ret: PartnerReturn, fr: FulfilmentRequest | None) -> dict:
    lines = {ln.line_id: ln for ln in (fr.lines if fr else [])}
    return {"return_id": str(ret.return_id), "return_no": ret.return_no,
            "request_id": str(ret.request_id), "request_no": fr.request_no if fr else None,
            "order_number": fr.order_number if fr else None,
            "customer_name": fr.customer_name if fr else None,
            "branch_id": str(ret.branch_id), "supplier_id": str(ret.supplier_id),
            "reason": ret.reason, "status": ret.status, "created_at": ret.created_at,
            "sent_at": ret.sent_at, "accepted_at": ret.accepted_at,
            "credit_total": str(ret.credit_total) if ret.credit_total is not None else None,
            "lines": [{"name": lines[rl.fr_line_id].name if rl.fr_line_id in lines else None,
                       "variant": (lines[rl.fr_line_id].variant_name
                                   if rl.fr_line_id in lines else None),
                       "qty": rl.qty} for rl in ret.lines]}


async def _returns(db, stmt) -> list[dict]:
    rows = list(await db.scalars(stmt.order_by(PartnerReturn.created_at.desc()).limit(200)))
    frs = {fr.request_id: fr for fr in await db.scalars(select(FulfilmentRequest).where(
        FulfilmentRequest.request_id.in_([r.request_id for r in rows])))} if rows else {}
    return [return_view(r, frs.get(r.request_id)) for r in rows]


async def staff_returns(db: AsyncSession, scope: set[uuid.UUID] | None, *,
                        status: str | None = "open") -> list[dict]:
    stmt = select(PartnerReturn)
    if scope is not None:
        stmt = stmt.where(PartnerReturn.branch_id.in_(scope))
    if status == "open":
        stmt = stmt.where(PartnerReturn.status != "accepted")
    elif status:
        stmt = stmt.where(PartnerReturn.status == status)
    return await _returns(db, stmt)


async def partner_returns(db: AsyncSession, supplier_id: uuid.UUID, *,
                          open_only: bool = True) -> list[dict]:
    stmt = select(PartnerReturn).where(PartnerReturn.supplier_id == supplier_id)
    if open_only:
        stmt = stmt.where(PartnerReturn.status != "accepted")
    return await _returns(db, stmt)


async def mark_sent(db: AsyncSession, actor: Actor, scope: set[uuid.UUID] | None,
                    return_id: uuid.UUID) -> dict:
    """Staff handed the goods to the partner."""
    ret = await db.scalar(select(PartnerReturn).where(PartnerReturn.return_id == return_id)
                          .with_for_update())
    if ret is None or (scope is not None and ret.branch_id not in scope):
        raise NotFound("return not found")
    if ret.status != "waiting":
        if ret.status == "sent":
            return return_view(ret, await db.get(FulfilmentRequest, ret.request_id))
        raise Conflict(f"this return is {ret.status}")
    ret.status, ret.sent_at, ret.sent_by = "sent", _now(), actor.user_id
    await audit.record(db, user_id=actor.user_id, action="partner.return_sent",
                       entity="partner_return", entity_id=ret.return_id,
                       after={"return_no": ret.return_no}, ip_address=actor.ip_address)
    await db.flush()
    return return_view(ret, await db.get(FulfilmentRequest, ret.request_id))


async def accept(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID,
                 return_id: uuid.UUID) -> dict:
    """The partner has its goods back: its stock count goes up; booked goods become a
    supplier credit (Finance)."""
    ret = await db.scalar(select(PartnerReturn).where(PartnerReturn.return_id == return_id)
                          .with_for_update())
    if ret is None or ret.supplier_id != supplier_id:
        raise NotFound("return not found")
    fr = await db.scalar(select(FulfilmentRequest)
                         .where(FulfilmentRequest.request_id == ret.request_id)
                         .with_for_update())
    if ret.status == "accepted":
        return return_view(ret, fr)
    if ret.status != "sent":
        raise Conflict("Sribees has not handed these goods to you yet")
    lines = {ln.line_id: ln for ln in fr.lines}
    for rl in ret.lines:
        fl = lines[rl.fr_line_id]
        item = await db.scalar(select(PartnerItem).where(
            PartnerItem.supplier_id == fr.supplier_id, PartnerItem.branch_id == fr.branch_id,
            PartnerItem.online_variant_id == fl.online_variant_id).with_for_update())
        if item is not None:
            item.stock_qty += rl.qty
    ret.status, ret.accepted_at, ret.accepted_by = "accepted", _now(), supplier_user_id
    if ret.in_books:
        await _credit(db, ret, fr, lines)
    await audit.record(db, supplier_user_id=supplier_user_id, action="partner.return_accepted",
                       entity="partner_return", entity_id=ret.return_id,
                       after={"return_no": ret.return_no,
                              "credit": str(ret.credit_total) if ret.credit_total else None})
    await db.flush()
    return return_view(ret, fr)


async def _credit(db, ret: PartnerReturn, fr: FulfilmentRequest, lines: dict) -> None:
    """Booked units: from the customer back into their batch at the cost they left at, then
    out to the partner at the same value; Finance makes the supplier credit."""
    when = _today()
    plan = []
    for rl in ret.lines:
        fl = lines[rl.fr_line_id]
        remaining = fl.out_qty - fl.back_qty
        n = min(rl.qty, remaining)
        if n <= 0 or fl.batch_id is None or fl.out_value is None:
            continue
        v = (fl.out_value - fl.back_value if n == remaining
             else stock.money(fl.out_value * n / fl.out_qty))
        plan.append((rl, fl, n, v))
    total = sum((v for *_x, v in plan), Decimal("0"))
    ret.credit_total = total
    if total <= 0:
        return
    label = fr.order_number or fr.request_no
    back = await _journal(db, source=SALES, branch_id=fr.branch_id, dr="1300", cr="5000",
                          value=total, when=when, ref=f"partner:return:{ret.return_id}:in",
                          label=ret.return_no,
                          memo=f"Online order {label}: partner goods back from the customer")
    out = await _journal(db, source=PURCHASING, branch_id=fr.branch_id, dr="2020", cr="1300",
                         value=total, when=when, ref=f"partner:return:{ret.return_id}:out",
                         label=ret.return_no,
                         memo=f"{ret.return_no}: goods returned to the partner ({label})")
    for rl, fl, n, v in plan:
        batch = await db.get(StockBatch, fl.batch_id)
        qty = Decimal(n) * fl.stock_per_unit  # pieces, or kilograms
        await stock.move(db, batch=batch, move_type="return", qty=qty, value=v,
                         move_date=when, doc_type="online_order", doc_id=fr.online_order_id,
                         user_id=None, journal_id=back.journal_id if back else None)
        await stock.move(db, batch=batch, move_type="supplier_return", qty=-qty,
                         value=-v, move_date=when, doc_type="supplier_return",
                         doc_id=ret.return_id, user_id=None,
                         journal_id=out.journal_id if out else None)
        fl.back_qty += n
        fl.back_value += v
        rl.value = v
    branch = await db.get(Branch, fr.branch_id)
    # the order the goods were bought on: a second price may have its own same-day order
    order_ids = {}
    for _rl, fl, _n, _v in plan:
        order_ids[fl.line_id] = await db.scalar(
            select(Receipt.order_id).join(ReceiptLine, ReceiptLine.receipt_id == Receipt.receipt_id)
            .where(ReceiptLine.batch_id == fl.batch_id))
    first = next(iter(order_ids.values()), None) or fr.supply_order_id
    order = await db.get(SupplyOrder, first) if first else None
    db.add(OutboxEvent(event="supplier_return.posted", payload={
        "return_id": str(ret.return_id), "return_no": ret.return_no,
        "supplier_id": str(fr.supplier_id), "branch_id": str(fr.branch_id),
        "online_branch_id": str(branch.online_branch_id) if branch else None,
        "order_id": str(order.order_id) if order else None,
        "order_no": order.order_no if order else None,
        "return_date": when.isoformat(), "total": str(total), "reason": ret.reason,
        "customer_order": fr.order_number, "request_no": fr.request_no,
        "journal_id": str(out.journal_id) if out else None,
        "lines": [{"product_id": str(fl.online_variant_id), "qty": n, "value": str(v),
                   "order_id": str(order_ids[fl.line_id]) if order_ids.get(fl.line_id) else None}
                  for _rl, fl, n, v in plan]}))


# ======================================================================= worker
async def due(db: AsyncSession, cutoff: date) -> list[tuple[uuid.UUID, date]]:
    """(branch, day) pairs with handed, unbooked requests from before `cutoff`."""
    start, _ = _day_bounds(cutoff)
    rows = (await db.execute(
        select(FulfilmentRequest.branch_id, FulfilmentRequest.handed_at).where(
            FulfilmentRequest.status == "handed", FulfilmentRequest.booked_at.is_(None),
            FulfilmentRequest.handed_at < start))).all()
    return sorted({(b, local_day(at)) for b, at in rows}, key=lambda x: (x[1], str(x[0])))


async def run_once(sessionmaker: async_sessionmaker, now: datetime | None = None) -> dict:
    """After midnight (Sri Lanka, plus a short grace): each earlier day's handed requests
    become that day's partner order. A day that cannot be booked is kept for staff and tried
    again next round; it never holds up the others (review #6)."""
    local = (now or datetime.now(UTC)).astimezone(COLOMBO)
    cutoff = local.date() if local.time() >= GRACE else local.date() - timedelta(days=1)
    async with sessionmaker() as db:
        pairs = await due(db, cutoff)
    done, problems = {}, {}
    for branch_id, day in pairs:
        try:
            async with sessionmaker() as db, db.begin():
                r = await close_day(db, branch_id, day)
                done[f"{branch_id}:{day}"] = r.get("orders")
                if r.get("problems"):  # another partner of that day still waits
                    problems[f"{branch_id}:{day}"] = "; ".join(r["problems"])[:300]
        except Exception as exc:  # noqa: BLE001 -- kept for staff, the next pair goes on
            problems[f"{branch_id}:{day}"] = str(getattr(exc, "message", exc))[:300]
            log.warning("partner day %s %s not booked: %s", branch_id, day,
                        problems[f"{branch_id}:{day}"])
    async with sessionmaker() as db, db.begin():
        state = await db.get(SyncState, STATE, with_for_update=True)
        value = {"problems": problems, "at": datetime.now(UTC).isoformat()}
        if state is None:
            db.add(SyncState(key=STATE, value=value))
        else:
            state.value = value
    return {"booked": done, "problems": problems} if (done or problems) else {}


async def status(db: AsyncSession, scope: set[uuid.UUID] | None) -> dict:
    """What needs a person: days not booked, bags that went out unscanned, returns waiting,
    partner events that failed."""
    from app.models import PartnerEventFailure  # noqa: PLC0415

    state = await db.get(SyncState, STATE)
    problems = dict((state.value or {}).get("problems") or {}) if state else {}
    if scope is not None:
        problems = {k: v for k, v in problems.items()
                    if any(k.startswith(f"{b}:") for b in scope)}
    stmt = select(FulfilmentRequest).where(FulfilmentRequest.unscanned_at.is_not(None),
                                           FulfilmentRequest.status.in_(HANDOVER_FROM))
    if scope is not None:
        stmt = stmt.where(FulfilmentRequest.branch_id.in_(scope))
    unscanned = [{"request_id": str(fr.request_id), "request_no": fr.request_no,
                  "order_number": fr.order_number, "branch_id": str(fr.branch_id),
                  "since": fr.unscanned_at} for fr in await db.scalars(stmt.limit(200))]
    waiting = select(func.count()).select_from(PartnerReturn).where(
        PartnerReturn.status != "accepted")
    if scope is not None:
        waiting = waiting.where(PartnerReturn.branch_id.in_(scope))
    failed = select(func.count()).select_from(PartnerEventFailure).where(
        PartnerEventFailure.resolved_at.is_(None))
    if scope is not None:
        failed = failed.where(PartnerEventFailure.branch_id.in_(scope))
    failures = await db.scalar(failed)
    return {"problems": problems, "unscanned": unscanned,
            "returns_open": await db.scalar(waiting) or 0,
            "event_failures": failures or 0}
