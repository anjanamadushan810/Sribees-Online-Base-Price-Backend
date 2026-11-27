"""Receipt drafts: barcode-first receiving in the staff app (plan 2026-11-27, P2).

Staff open the order, then take the delivery in one product at a time: scan the pack's
barcode, enter units / MRP / expiry, scan exactly one QR label per unit. Each product is
saved here at once. "Finish" (bill number, total, transport) makes the real receipt through
receiving.receive, so the stock, base price, Finance and Online rules stay in one place.

A QR can never count twice (owner 2026-11-27): every scan reserves the label for this draft
straight away (the label number is the primary key of receipt_draft_labels), a label already
on a product of this draft or held by another draft is refused, and at finish each label
moves printed -> linked under a row lock in labels.link.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Label,
    OnlineProduct,
    OnlineVariant,
    Receipt,
    ReceiptDraft,
    ReceiptDraftLabel,
    ReceiptDraftLine,
    SupplyOrder,
    SupplyOrderLine,
)
from app.services import audit, barcodes, labels, receiving
from app.services.actors import Actor
from app.services.errors import Conflict, NotFound, SupplyError

MAX_LINES = 500
MAX_EXPIRY_YEARS = 10


def colombo_today() -> date:
    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()


def check_expiry(expiry: date | None, received: date, mfg: date | None = None,
                 name: str = "this product") -> None:
    """Expiry is required on every line (owner 2026-11-27) and must be after arrival."""
    if expiry is None:
        raise SupplyError(f"{name}: choose the expiry date")
    if expiry <= received:
        raise SupplyError(f"{name}: the expiry date {expiry} is not after {received}; "
                          "expired goods are not taken into stock")
    if expiry > received + timedelta(days=365 * MAX_EXPIRY_YEARS + 3):  # 29 Feb safe
        raise SupplyError(f"{name}: an expiry more than {MAX_EXPIRY_YEARS} years ahead "
                          "looks like a typing mistake")
    if mfg is not None and (mfg >= expiry or mfg > received):
        raise SupplyError(f"{name}: the manufacture date must be before the expiry and "
                          "not after today")


# ---------------------------------------------------------------- open / read
async def _order(db: AsyncSession, order_id: uuid.UUID,
                 scope: set[uuid.UUID] | None) -> SupplyOrder:
    order = await db.get(SupplyOrder, order_id)
    if order is None or (scope is not None and order.branch_id not in scope):
        raise NotFound("order not found")
    return order


async def open_draft(db: AsyncSession, actor: Actor, order_id: uuid.UUID,
                     scope: set[uuid.UUID] | None) -> ReceiptDraft:
    """The order's open draft, made when there is none (two phones share one)."""
    order = await db.get(SupplyOrder, order_id, with_for_update=True, populate_existing=True)
    if order is None or (scope is not None and order.branch_id not in scope):
        raise NotFound("order not found")
    quick = order.is_quick_buy and order.status == "submitted"
    if order.status not in receiving.RECEIVABLE and not quick:
        raise Conflict(f"order {order.order_no} is {order.status}; it cannot be received")
    draft = (await db.scalars(select(ReceiptDraft).where(
        ReceiptDraft.order_id == order_id, ReceiptDraft.status == "open"))).first()
    if draft is None:
        draft = ReceiptDraft(draft_id=uuid.uuid4(), order_id=order_id,
                             created_by=actor.user_id)
        db.add(draft)
        await db.flush()
        await audit.record(db, user_id=actor.user_id, action="receipt_draft.open",
                           entity="receipt_draft", entity_id=draft.draft_id,
                           after={"order_no": order.order_no}, ip_address=actor.ip_address)
    return draft


async def get_draft(db: AsyncSession, draft_id: uuid.UUID, scope: set[uuid.UUID] | None, *,
                    lock: bool = False, open_only: bool = True) -> tuple[ReceiptDraft,
                                                                         SupplyOrder]:
    draft = await db.get(ReceiptDraft, draft_id, with_for_update=lock)
    if draft is None:
        raise NotFound("draft not found")
    order = await _order(db, draft.order_id, scope)
    if open_only and draft.status != "open":
        raise Conflict(f"this delivery was already {draft.status}")
    return draft, order


async def _draft_units(db: AsyncSession, draft_id: uuid.UUID,
                       exclude: uuid.UUID | None = None) -> dict[uuid.UUID, int]:
    q = (select(ReceiptDraftLine.order_line_id, func.sum(ReceiptDraftLine.units))
         .where(ReceiptDraftLine.draft_id == draft_id,
                ReceiptDraftLine.order_line_id.is_not(None))
         .group_by(ReceiptDraftLine.order_line_id))
    if exclude is not None:
        q = q.where(ReceiptDraftLine.draft_line_id != exclude)
    return {k: int(v) for k, v in (await db.execute(q)).all()}


async def view(db: AsyncSession, draft: ReceiptDraft, order: SupplyOrder) -> dict:
    await db.refresh(order, ["lines"])
    got = await receiving.received_units(db, order.order_id)
    taking = await _draft_units(db, draft.draft_id)
    lines = list(await db.scalars(select(ReceiptDraftLine).where(
        ReceiptDraftLine.draft_id == draft.draft_id).order_by(ReceiptDraftLine.created_at)))
    held = list(await db.scalars(select(ReceiptDraftLabel).where(
        ReceiptDraftLabel.draft_id == draft.draft_id).order_by(ReceiptDraftLabel.reserved_at)))
    by_line: dict[uuid.UUID | None, list[str]] = {}
    for h in held:
        by_line.setdefault(h.draft_line_id, []).append(h.number)
    names = {ol.line_id: f"{ol.heading} {ol.label or ''}".strip() for ol in order.lines}
    extra_names = {}
    extra_ids = {ln.online_variant_id for ln in lines if ln.order_line_id is None}
    for vid in extra_ids:
        item = await barcodes._item(db, vid)
        extra_names[vid] = f"{item.name} {item.variant}".strip() if item else "?"
    return {
        "draft_id": draft.draft_id, "order_id": order.order_id, "order_no": order.order_no,
        "status": draft.status, "receipt_id": draft.receipt_id,
        "order_lines": [{
            "order_line_id": ol.line_id, "name": names[ol.line_id],
            "online_variant_id": ol.online_variant_id, "ordered": ol.units,
            "received": got.get(ol.line_id, 0), "in_draft": taking.get(ol.line_id, 0),
            "due": max(ol.units - got.get(ol.line_id, 0) - taking.get(ol.line_id, 0), 0),
            "unit_price": f"{ol.unit_price:.2f}",
            "mrp": None if ol.mrp is None else f"{ol.mrp:.2f}",
        } for ol in order.lines],
        "lines": [{
            "draft_line_id": ln.draft_line_id, "order_line_id": ln.order_line_id,
            "online_variant_id": ln.online_variant_id, "barcode": ln.barcode,
            "name": names.get(ln.order_line_id) or extra_names.get(ln.online_variant_id),
            "added_at_receiving": ln.order_line_id is None,
            "units": ln.units, "free_units": ln.free_units,
            "weight_kg": None if ln.weight_kg is None else str(ln.weight_kg),
            "expiry_date": ln.expiry_date, "mfg_date": ln.mfg_date,
            "mrp": None if ln.mrp is None else f"{ln.mrp:.2f}",
            "unit_price": None if ln.unit_price is None else f"{ln.unit_price:.2f}",
            "labels": by_line.get(ln.draft_line_id, []),
        } for ln in lines],
        "loose_labels": by_line.get(None, []),
    }


# ---------------------------------------------------------------- QR labels
async def reserve(db: AsyncSession, actor: Actor, draft: ReceiptDraft, raw: str) -> str:
    """Check one scanned QR at once and hold it for this draft."""
    number = labels.parse_number(raw)
    row = await db.get(Label, number, with_for_update=True)  # two phones: one at a time
    if row is None:
        raise SupplyError(f"label {number} was never printed by Sribees Supply")
    if row.status != "printed":
        raise Conflict(f"label {number} is already {row.status} on a batch: use a new label")
    held = await db.get(ReceiptDraftLabel, number)
    if held is not None:
        if held.draft_id != draft.draft_id:
            raise Conflict(f"label {number} is held by another delivery being received")
        if held.draft_line_id is not None:
            line = await db.get(ReceiptDraftLine, held.draft_line_id)
            raise Conflict(f"label {number} is already scanned on another product of this "
                           f"delivery ({line.units if line else '?'} units saved)")
        return number  # scanned again before the product was saved: no change
    db.add(ReceiptDraftLabel(number=number, draft_id=draft.draft_id, reserved_by=actor.user_id))
    await db.flush()
    return number


async def release(db: AsyncSession, draft: ReceiptDraft, raw: str) -> None:
    number = labels.parse_number(raw)
    held = await db.get(ReceiptDraftLabel, number)
    if held is None or held.draft_id != draft.draft_id:
        raise NotFound("label not held by this delivery")
    if held.draft_line_id is not None:
        raise Conflict(f"label {number} is on a saved product: remove that product first")
    await db.delete(held)
    await db.flush()


# ---------------------------------------------------------------- products
@dataclass(frozen=True)
class DraftLineIn:
    order_line_id: uuid.UUID | None
    online_variant_id: uuid.UUID | None
    units: int
    expiry_date: date | None
    labels: tuple[str, ...]
    free_units: int = 0
    weight_kg: Decimal | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = None
    unit_price: Decimal | None = None
    listed_price: Decimal | None = None
    selling_price: Decimal | None = None
    barcode: str | None = None


async def _resolve(db: AsyncSession, order: SupplyOrder,
                   body: DraftLineIn) -> tuple[SupplyOrderLine | None, uuid.UUID | None, str]:
    """(order line, extra product, name). A scanned pack on the order -> its line."""
    await db.refresh(order, ["lines"])
    if body.order_line_id is not None:
        ol = next((x for x in order.lines if x.line_id == body.order_line_id), None)
        if ol is None:
            raise SupplyError("that line is not on this order")
        return ol, None, f"{ol.heading} {ol.label or ''}".strip()
    if body.online_variant_id is None:
        raise SupplyError("scan the product or choose its line")
    ol = next((x for x in order.lines if x.online_variant_id == body.online_variant_id), None)
    if ol is not None:
        return ol, None, f"{ol.heading} {ol.label or ''}".strip()
    variant = await db.get(OnlineVariant, body.online_variant_id)
    product = await db.get(OnlineProduct, variant.online_product_id) if variant else None
    if variant is None or not variant.is_active or product is None or not product.is_active:
        raise SupplyError("that product is not sold on SRIBEES Online; the super admin adds "
                          "new products there first")
    if body.unit_price is None:
        raise SupplyError(f"{product.name} was not ordered: enter its bill price")
    return None, variant.online_variant_id, f"{product.name} {variant.name or ''}".strip()


async def add_line(db: AsyncSession, actor: Actor, draft: ReceiptDraft, order: SupplyOrder,
                   body: DraftLineIn) -> ReceiptDraftLine:
    count = await db.scalar(select(func.count()).select_from(ReceiptDraftLine).where(
        ReceiptDraftLine.draft_id == draft.draft_id)) or 0
    if count >= MAX_LINES:
        raise SupplyError(f"at most {MAX_LINES} products in one delivery")
    ol, extra, name = await _resolve(db, order, body)
    if not (0 < body.units <= receiving.MAX_UNITS) or not (0 <= body.free_units
                                                          <= receiving.MAX_UNITS):
        raise SupplyError(f"{name}: units must be above zero")
    # owner 2026-11-27: a supplier may send more than was ordered; it is taken (the app
    # asks staff to confirm), and the bill total shows it to the accountant
    check_expiry(body.expiry_date, colombo_today(), body.mfg_date, name)
    if body.mrp is not None and body.mrp < 0:
        raise SupplyError(f"{name}: the MRP cannot be below zero")
    numbers = [labels.parse_number(n) for n in body.labels]
    if len(set(numbers)) != len(numbers):
        raise SupplyError(f"{name}: a QR label was scanned twice")
    pieces = body.units + body.free_units
    if body.weight_kg is None and len(numbers) != pieces:
        raise SupplyError(f"{name}: {pieces} units need {pieces} QR labels, "
                          f"{len(numbers)} scanned")
    if body.weight_kg is not None and not numbers:
        raise SupplyError(f"{name}: scan the QR label on each bag")
    code = barcodes.normalise(body.barcode) if body.barcode else None
    for n in numbers:
        await reserve(db, actor, draft, n)  # refuses one already on another product
    line = ReceiptDraftLine(
        draft_line_id=uuid.uuid4(), draft_id=draft.draft_id,
        order_line_id=ol.line_id if ol else None, online_variant_id=extra, barcode=code,
        units=body.units, free_units=body.free_units, weight_kg=body.weight_kg,
        expiry_date=body.expiry_date, mfg_date=body.mfg_date, mrp=body.mrp,
        unit_price=body.unit_price, listed_price=body.listed_price,
        selling_price=body.selling_price, created_by=actor.user_id)
    db.add(line)
    await db.flush()
    for n in numbers:
        held = await db.get(ReceiptDraftLabel, n)
        held.draft_line_id = line.draft_line_id
    draft.updated_at = datetime.now(UTC)
    await db.flush()
    return line


async def remove_line(db: AsyncSession, draft: ReceiptDraft, line_id: uuid.UUID) -> None:
    """Take a saved product off the delivery; its QR labels are free again."""
    line = await db.get(ReceiptDraftLine, line_id)
    if line is None or line.draft_id != draft.draft_id:
        raise NotFound("product not on this delivery")
    await db.execute(delete(ReceiptDraftLabel).where(
        ReceiptDraftLabel.draft_line_id == line_id))
    await db.delete(line)
    draft.updated_at = datetime.now(UTC)
    await db.flush()


# ---------------------------------------------------------------- finish / discard
async def finish(db: AsyncSession, actor: Actor, draft: ReceiptDraft, *, received_date: date,
                 invoice_no: str | None, invoice_total: Decimal | None, notes: str | None,
                 transport_charge: Decimal | None,
                 scope: set[uuid.UUID] | None) -> Receipt:
    lines = list(await db.scalars(select(ReceiptDraftLine).where(
        ReceiptDraftLine.draft_id == draft.draft_id).order_by(ReceiptDraftLine.created_at)))
    if not lines:
        raise SupplyError("nothing saved on this delivery yet")
    held = list(await db.scalars(select(ReceiptDraftLabel).where(
        ReceiptDraftLabel.draft_id == draft.draft_id)))
    loose = [h.number for h in held if h.draft_line_id is None]
    if loose:
        raise SupplyError(f"{len(loose)} scanned QR labels are not on any product: save that "
                          "product or remove them")
    of_line: dict[uuid.UUID, list[str]] = {}
    for h in held:
        of_line.setdefault(h.draft_line_id, []).append(h.number)
    for ln in lines:
        check_expiry(ln.expiry_date, received_date, ln.mfg_date)
    receipt = await receiving.receive(
        db, actor, draft.order_id, received_date=received_date, invoice_no=invoice_no,
        invoice_total=invoice_total, notes=notes, transport_charge=transport_charge,
        scope=scope, split_lines=True, draft_id=draft.draft_id,
        lines=[receiving.ReceiptLineIn(
            order_line_id=ln.order_line_id, online_variant_id=ln.online_variant_id,
            units=ln.units, free_units=ln.free_units, weight_kg=ln.weight_kg,
            expiry_date=ln.expiry_date, mfg_date=ln.mfg_date, mrp=ln.mrp,
            unit_price=ln.unit_price, listed_price=ln.listed_price,
            selling_price=ln.selling_price,
            labels=tuple(sorted(of_line.get(ln.draft_line_id, []))))
            for ln in lines])
    await db.execute(delete(ReceiptDraftLabel).where(ReceiptDraftLabel.draft_id == draft.draft_id))
    draft.status, draft.receipt_id = "finished", receipt.receipt_id
    draft.closed_by, draft.closed_at = actor.user_id, datetime.now(UTC)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="receipt_draft.finish",
                       entity="receipt_draft", entity_id=draft.draft_id,
                       after={"receipt_no": receipt.receipt_no, "lines": len(lines),
                              "labels": len(held)}, ip_address=actor.ip_address)
    return receipt


async def discard(db: AsyncSession, actor: Actor, draft: ReceiptDraft) -> None:
    n = await db.scalar(select(func.count()).select_from(ReceiptDraftLine).where(
        ReceiptDraftLine.draft_id == draft.draft_id)) or 0
    await db.execute(delete(ReceiptDraftLabel).where(ReceiptDraftLabel.draft_id == draft.draft_id))
    draft.status = "discarded"
    draft.closed_by, draft.closed_at = actor.user_id, datetime.now(UTC)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="receipt_draft.discard",
                       entity="receipt_draft", entity_id=draft.draft_id,
                       after={"lines": n}, ip_address=actor.ip_address)
