"""Pack scan (joint-go-live plan, phase E; decisions 24 and 25).

Online's packer scans each unit's QR label. Supply checks the label belongs to that order
line's pack size, at that order's branch, from a batch that is not expired and still has
the unit free, and holds it for the order (a pick). Online's order events then settle the
picks: delivered = the stock leaves at the batch's cost, cancelled = the unit is free
again, returned / refused = it goes back into the same batch.

    delivered  Dr 5000 Cost of goods sold   Cr 1300 Inventory   (source "sales")
    returned   Dr 1300 Inventory            Cr 5000 Cost of goods sold
"""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, Label, Pick, Product, StockBatch, StockMove
from app.services import labels as label_svc
from app.services import ledger, stock
from app.services.errors import Conflict, NotFound, SupplyError

SOURCE = "sales"
ZERO = Decimal("0")
WEIGHT_SLACK = Decimal("1.10")  # a weighed line may be packed up to 10 % over


def colombo_today() -> date:
    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()


async def _reserved(db: AsyncSession, batch_id: uuid.UUID) -> Decimal:
    return await db.scalar(select(func.coalesce(func.sum(Pick.qty), 0)).where(
        Pick.batch_id == batch_id, Pick.status == "picked")) or ZERO


async def _item_picks(db: AsyncSession, order_id: uuid.UUID, item_id: uuid.UUID,
                      statuses=("picked",)) -> list[Pick]:
    return list(await db.scalars(select(Pick).where(
        Pick.online_order_id == order_id, Pick.order_item_id == item_id,
        Pick.status.in_(statuses)).order_by(Pick.picked_at)))


async def pick(db: AsyncSession, *, order_id: uuid.UUID, order_number: str | None,
               item_id: uuid.UUID, online_branch_id: uuid.UUID, variant_id: uuid.UUID | None,
               ordered_qty: int, weight_grams: int | None, raw_label: str,
               weight_kg: Decimal | None, picked_by: str | None) -> dict:
    number = label_svc.parse_number(raw_label)
    label = await db.get(Label, number, with_for_update=True)
    if label is None:
        raise NotFound(f"label {number} was never printed by Sribees Supply")
    if label.status == "printed":
        raise SupplyError(f"label {number} is not on any stock yet; it is linked when the "
                          "goods are received")
    if label.status == "void":
        raise Conflict(f"label {number} is void")
    if variant_id is None:
        raise SupplyError("this order line has no pack size; it cannot be scanned")
    batch = await db.get(StockBatch, label.batch_id, with_for_update=True)
    product = await db.get(Product, batch.product_id)
    if product.product_id != variant_id:
        raise SupplyError(f"label {number} is on {product.name}, not on this item")
    branch = await db.get(Branch, batch.branch_id)
    if branch is None or branch.online_branch_id != online_branch_id:
        raise SupplyError(f"label {number} is stock of another branch")
    if batch.status != "available":
        raise Conflict(f"batch {batch.batch_no} is in {batch.status}; it cannot be sold")
    today = colombo_today()
    if batch.expiry_date is not None and batch.expiry_date < today:
        raise SupplyError(f"{product.name} from this batch expired on {batch.expiry_date}")
    if not (0 < ordered_qty <= 100_000):
        raise SupplyError("the order line quantity is not valid")

    free = batch.qty_on_hand - await _reserved(db, batch.batch_id)
    open_picks = await _item_picks(db, order_id, item_id)
    by_weight = product.unit == "kg"
    if by_weight:
        if weight_kg is None or weight_kg <= 0:
            raise SupplyError(f"{product.name} is sold by weight: enter the kilograms taken "
                              "from this bag")
        qty = stock.quantity(weight_kg, product)
        recent = open_picks[-1] if open_picks else None
        if (recent is not None and recent.label == number and recent.qty == qty
                and datetime.now(UTC) - recent.picked_at < timedelta(seconds=20)):
            # the same bag and kilograms again at once: a retry after a timeout (review #8)
            return await item_summary(db, order_id, item_id)
        if weight_grams:
            need = Decimal(ordered_qty * weight_grams) / 1000
            done = sum((p.qty for p in open_picks), ZERO)
            if done + qty > need * WEIGHT_SLACK:
                raise SupplyError(f"{product.name}: {done + qty} kg is more than the "
                                  f"{need} kg ordered")
    else:
        if weight_kg is not None:
            raise SupplyError(f"{product.name} is counted in pieces, not weighed")
        if label.status == "used":
            raise Conflict(f"label {number} was already sold")
        taken = await db.scalar(select(Pick).where(
            Pick.label == number, Pick.status.in_(("picked", "consumed"))))
        if taken is not None:
            where = ("this order" if taken.online_order_id == order_id
                     else f"order {taken.order_number or taken.online_order_id}")
            raise Conflict(f"label {number} is already scanned for {where}")
        if len(open_picks) >= ordered_qty:
            raise SupplyError(f"all {ordered_qty} of {product.name} are already scanned")
        qty = Decimal("1")
    if qty > free:
        raise SupplyError(f"batch {batch.batch_no} has only {free} free; count the stock")
    db.add(Pick(online_order_id=order_id, order_number=order_number, order_item_id=item_id,
                online_branch_id=online_branch_id, product_id=product.product_id,
                label=number, batch_id=batch.batch_id, qty=qty, by_weight=by_weight,
                picked_by=(picked_by or "")[:120] or None))
    await db.flush()
    return await item_summary(db, order_id, item_id)


async def unpick(db: AsyncSession, *, order_id: uuid.UUID, raw_label: str) -> int:
    """Take a mistaken scan back (only while the order is not settled)."""
    number = label_svc.parse_number(raw_label)
    rows = list(await db.scalars(select(Pick).where(
        Pick.online_order_id == order_id, Pick.label == number, Pick.status == "picked")
        .order_by(Pick.picked_at.desc()).with_for_update()))
    if not rows:
        raise NotFound(f"label {number} is not scanned for this order")
    rows[0].status, rows[0].settled_at = "released", datetime.now(UTC)
    await db.flush()
    return 1


async def item_summary(db: AsyncSession, order_id: uuid.UUID, item_id: uuid.UUID) -> dict:
    rows = await _item_picks(db, order_id, item_id)
    return {"order_item_id": str(item_id), "scanned_units": sum(1 for p in rows if not p.by_weight),
            "scanned_kg": str(sum((p.qty for p in rows if p.by_weight), ZERO)),
            "labels": [p.label for p in rows]}


async def order_summary(db: AsyncSession, order_id: uuid.UUID) -> dict:
    rows = list(await db.scalars(select(Pick).where(
        Pick.online_order_id == order_id, Pick.status.in_(("picked", "consumed")))
        .order_by(Pick.picked_at)))
    items: dict[str, dict] = {}
    for p in rows:
        it = items.setdefault(str(p.order_item_id), {
            "order_item_id": str(p.order_item_id), "scanned_units": 0, "scanned_kg": ZERO,
            "labels": []})
        if p.by_weight:
            it["scanned_kg"] += p.qty
        else:
            it["scanned_units"] += 1
        it["labels"].append(p.label)
    for it in items.values():
        it["scanned_kg"] = str(it["scanned_kg"])
    return {"online_order_id": str(order_id), "items": list(items.values())}


# ---------------------------------------------------------------- settling (Online events)
async def _journal(db, *, order_id, number, branch_values: dict, returned: bool, when: date,
                   ref: str):
    lines = []
    for branch_id, value in branch_values.items():
        if value <= 0:
            continue
        dr, cr = ("1300", "5000") if returned else ("5000", "1300")
        lines += [ledger.LineIn(dr, branch_id, debit=value, memo=number),
                  ledger.LineIn(cr, branch_id, credit=value, memo=number)]
    if not lines:
        return None
    what = "returned to stock" if returned else "cost of goods delivered"
    return await ledger.post_journal(db, entry_date=when, source=SOURCE, source_ref=ref,
                                     memo=f"Online order {number or order_id}: {what}",
                                     lines=lines, user_id=None)


async def consume(db: AsyncSession, order_id: uuid.UUID, number: str | None,
                  when: date, ref: str | None = None) -> int:
    rows = list(await db.scalars(select(Pick).where(
        Pick.online_order_id == order_id, Pick.status == "picked")
        .order_by(Pick.picked_at).with_for_update()))
    if not rows:
        return 0
    batches = {p.batch_id: await db.get(StockBatch, p.batch_id) for p in rows}
    # value first (the batch's average remaining cost), then one journal, then the moves
    per_batch: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for p in rows:
        per_batch[p.batch_id] += p.qty
    values = {bid: await stock.issue_value(db, batches[bid], qty, for_picks=True)
              for bid, qty in per_batch.items()}
    by_branch: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for bid, v in values.items():
        by_branch[batches[bid].branch_id] += -v
    journal = await _journal(db, order_id=order_id, number=number, branch_values=by_branch,
                             returned=False, when=when,
                             ref=ref or f"online:{order_id}:delivered")
    now = datetime.now(UTC)
    for bid, qty in per_batch.items():
        mv = await stock.move(db, batch=batches[bid], move_type="dispatch", qty=-qty,
                              value=values[bid], move_date=when, doc_type="online_order",
                              doc_id=order_id, user_id=None,
                              journal_id=journal.journal_id if journal else None)
        for p in rows:
            if p.batch_id == bid:
                p.status, p.settled_at, p.move_id = "consumed", now, mv.move_id
                p.journal_id = journal.journal_id if journal else None
    for p in rows:
        if not p.by_weight:
            label = await db.get(Label, p.label, with_for_update=True)
            label.status, label.used_at = "used", now
            label.used_ref = f"online:{number or order_id}"[:80]
        else:
            batch = batches[p.batch_id]
            if batch.qty_on_hand == 0:  # the bag is empty
                label = await db.get(Label, p.label, with_for_update=True)
                label.status, label.used_at = "used", now
                label.used_ref = f"online:{number or order_id}"[:80]
    await db.flush()
    return len(rows)


async def release(db: AsyncSession, order_id: uuid.UUID) -> int:
    rows = list(await db.scalars(select(Pick).where(
        Pick.online_order_id == order_id, Pick.status == "picked").with_for_update()))
    now = datetime.now(UTC)
    for p in rows:
        p.status, p.settled_at = "released", now
    await db.flush()
    return len(rows)


async def give_back(db: AsyncSession, order_id: uuid.UUID, number: str | None,
                    items: list[dict], ordered: dict[str, int], when: date, why: str,
                    ref: str | None = None) -> int:
    """Returned or refused goods back into the batches they left from, at the cost they
    left at. `items` = [{order_item_id, quantity}]. Never more than went out (a pick keeps
    how much already came back: qty_returned)."""
    back: list[tuple[Pick, Decimal]] = []
    # units of each order line taken back from OUR shelf by this event: the fulfilment
    # partner's part of the same event takes only the rest (review 2026-11-28 #3)
    own_back = db.info.setdefault("own_back", {})
    for it in items:
        item_id = uuid.UUID(str(it["order_item_id"]))
        want = int(it.get("quantity") or 0)
        if want <= 0:
            continue
        out = [p for p in reversed(await _item_picks(db, order_id, item_id, ("consumed",)))
               if p.qty_returned < p.qty]
        pieces = [p for p in out if not p.by_weight]
        bags = [p for p in out if p.by_weight]
        back += [(p, p.qty - p.qty_returned) for p in pieces[:want]]
        key = f"{order_id}:{item_id}"
        own_back[key] = own_back.get(key, 0) + (want if bags else len(pieces[:want]))
        if bags:
            total = Decimal(max(ordered.get(str(item_id), want), 1))
            share = min(Decimal(want), total) / total
            for p in bags:
                q = min(p.qty - p.qty_returned, (p.qty * share).quantize(Decimal("0.001")))
                back.append((p, q))
    back = [(p, q) for p, q in back if q > 0]
    if not back:
        return 0
    by_branch: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    plan = []
    for p, q in back:
        mv = await db.get(StockMove, p.move_id)
        batch = await db.get(StockBatch, p.batch_id)
        moved = await db.scalar(select(func.coalesce(func.sum(Pick.qty), 0)).where(
            Pick.move_id == p.move_id)) or q
        value = stock.money(-mv.value * q / moved)
        plan.append((p, q, batch, value))
        by_branch[batch.branch_id] += value
    journal = await _journal(db, order_id=order_id, number=number, branch_values=by_branch,
                             returned=True, when=when, ref=ref or f"online:{order_id}:{why}")
    now = datetime.now(UTC)
    for p, q, batch, value in plan:
        mv = await stock.move(db, batch=batch, move_type="return", qty=q, value=value,
                              move_date=when, doc_type="online_order", doc_id=order_id,
                              user_id=None, journal_id=journal.journal_id if journal else None)
        p.qty_returned += q
        if p.qty_returned == p.qty:
            p.status = "returned"
        p.return_move_id, p.settled_at = mv.move_id, now
        label = await db.get(Label, p.label, with_for_update=True)
        if not p.by_weight or label.status == "used":
            label.status, label.used_at, label.used_ref = "linked", None, None
    await db.flush()
    return len(plan)


def _event_date(p: dict, key: str) -> date:
    raw = p.get(key)
    if raw:
        try:
            return (datetime.fromisoformat(raw).astimezone(UTC)
                    + timedelta(hours=5, minutes=30)).date()
        except ValueError:
            pass
    return colombo_today()


async def apply_event(db: AsyncSession, ev: dict) -> str:
    """One Online event (order.status / order.undelivered). Returns what it did. Journal
    references carry the event, so a second delivery after an override is booked too
    (review #6)."""
    p = ev.get("payload") or {}
    if ev.get("type") not in ("order.status", "order.undelivered") or not p.get("order_id"):
        return "ignored"
    order_id = uuid.UUID(p["order_id"])
    number = p.get("order_number")
    key = ev.get("event_id") or ev.get("seq")

    def ref(why: str) -> str:
        return f"online:{order_id}:{key}" if key else f"online:{order_id}:{why}"
    ordered = {ln["order_item_id"]: int(ln.get("quantity") or 0) for ln in p.get("lines") or []}
    if ev["type"] == "order.undelivered":
        n = await give_back(db, order_id, number, p.get("undelivered") or [], ordered,
                            colombo_today(), "undelivered", ref=ref("undelivered"))
        return f"undelivered:{n}"
    to = p.get("to_status")
    if to == "delivered":
        n = await consume(db, order_id, number, _event_date(p, "delivered_at"),
                          ref=ref("delivered"))
        return f"consumed:{n}"
    if to in ("cancelled", "rto_initiated"):
        # never delivered: what was held goes back to the shelf (RTO = the parcel returns
        # to the shop, review #4)
        return f"released:{await release(db, order_id)}"
    if to == "refunded":
        n = 0
        # only goods that physically came back return to stock; a refund without a
        # pick-up is money only (review #3)
        if p.get("returned") and p.get("return_received_at"):
            n = await give_back(db, order_id, number, p["returned"], ordered,
                                _event_date(p, "return_received_at"), "returned",
                                ref=ref("returned"))
        await release(db, order_id)
        return f"returned:{n}"
    return "ignored"
