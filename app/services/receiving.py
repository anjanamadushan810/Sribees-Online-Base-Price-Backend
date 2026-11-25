"""Receiving goods against a supply order (joint-go-live plan, phase B; replaces Finance's
GRN, which this follows line for line).

    receipt   Dr 1300 Inventory (branch)   Cr 2020 Goods received not invoiced

Each received line becomes a batch at its landed cost (paid value over paid + free
quantity). A short delivery is a back-order (decision 31): the order stays open for the
rest, as many receipts as needed, until every unit has arrived or staff close the rest
with a reason. The supplier's invoice number and total are entered here (decision 23);
Finance matches them three ways from the `receipt.posted` event.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Branch,
    OnlineProduct,
    OnlineVariant,
    OutboxEvent,
    Product,
    Receipt,
    ReceiptLine,
    Supplier,
    SupplyOrder,
    SupplyOrderLine,
)
from app.services import audit, books, labels, ledger, settings, stock
from app.services.actors import Actor, _now
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

SOURCE = "purchasing"  # Finance's source for goods received (same accounts as its GRN)
ZERO = Decimal("0")
MAX_UNITS = 1_000_000
# A supplier with the app accepts the order first; a supplier without it (a market cash
# buy, decision 21) cannot, so its approved order is received directly.
RECEIVABLE = ("accepted", "approved")


@dataclass(frozen=True)
class ReceiptLineIn:
    order_line_id: uuid.UUID
    units: int
    free_units: int = 0
    weight_kg: Decimal | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = None
    product_id: uuid.UUID | None = None
    labels: tuple[str, ...] = ()  # QR label numbers stuck on these units (phase C)


# ---------------------------------------------------------------- stock products
async def stock_item_for(db: AsyncSession, line: SupplyOrderLine) -> uuid.UUID | None:
    """The stock product of an order line: its pack size (Online variant), or the only
    pack size of its product. Same rule as the finance_product_id on order.submitted."""
    if line.online_variant_id is not None:
        return line.online_variant_id
    if line.online_product_id is None:
        return None
    ids = list(await db.scalars(select(OnlineVariant.online_variant_id).where(
        OnlineVariant.online_product_id == line.online_product_id)))
    return ids[0] if len(ids) == 1 else None


async def ensure_product(db: AsyncSession, product_id: uuid.UUID, *,
                         by_weight: bool = False) -> Product:
    """The stock product for an Online variant, created on first receipt from the
    catalogue mirror: kept by weight (kg, loose) when its first receipt gives kilograms,
    else counted in pieces (review 2026-11-13 #4: never guess pieces for weighed goods)."""
    product = await db.get(Product, product_id)
    if product is not None:
        return product
    variant = await db.get(OnlineVariant, product_id)
    if variant is None:
        raise SupplyError("this item is not an Online pack size; sync the Online catalogue "
                          "or choose the product")
    parent = await db.get(OnlineProduct, variant.online_product_id)
    name = parent.name if parent else variant.name
    if variant.name and parent and variant.name.lower() not in parent.name.lower():
        name = f"{parent.name} {variant.name}"
    product = Product(product_id=product_id, sku=variant.sku or (parent.sku if parent else None),
                      name=name[:255], unit="kg" if by_weight else "each",
                      stock_mode="loose" if by_weight else "unit", kind="sellable",
                      weight_kg=Decimal("1") if by_weight else (
                          Decimal(variant.weight_grams) / 1000 if variant.weight_grams
                          else None))
    db.add(product)
    await db.flush()
    return product


# ---------------------------------------------------------------- receiving
async def received_units(db: AsyncSession, order_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Paid units received so far per order line."""
    rows = await db.execute(
        select(ReceiptLine.order_line_id, func.coalesce(func.sum(ReceiptLine.units), 0))
        .join(Receipt, Receipt.receipt_id == ReceiptLine.receipt_id)
        .where(Receipt.order_id == order_id).group_by(ReceiptLine.order_line_id))
    return {line_id: int(units) for line_id, units in rows}


def _outstanding(order: SupplyOrder, got: dict[uuid.UUID, int]) -> int:
    return sum(max(ln.units - got.get(ln.line_id, 0), 0) for ln in order.lines)


async def _order(db: AsyncSession, actor: Actor, order_id: uuid.UUID,
                 scope: set[uuid.UUID] | None) -> SupplyOrder:
    order = await db.get(SupplyOrder, order_id, with_for_update=True)
    if order is None or (scope is not None and order.branch_id not in scope):
        raise NotFound("order not found")
    await db.refresh(order, ["lines"])
    return order


async def receive(
    db: AsyncSession,
    actor: Actor,
    order_id: uuid.UUID,
    *,
    received_date: date,
    invoice_no: str | None,
    invoice_total: Decimal | None,
    notes: str | None,
    lines: list[ReceiptLineIn],
    scope: set[uuid.UUID] | None = None,
) -> Receipt:
    order = await _order(db, actor, order_id, scope)
    supplier = await db.get(Supplier, order.supplier_id)
    if order.status not in RECEIVABLE:
        raise Conflict(f"order {order.order_no} is {order.status}; goods are received against "
                       "an order the supplier accepted")
    if order.status == "approved" and any(u.is_active for u in supplier.users):
        raise Conflict(f"{supplier.name} has not accepted order {order.order_no} yet")
    submitted = order.submitted_at.date() if order.submitted_at else None
    if submitted and received_date < submitted - timedelta(days=1):
        raise SupplyError("goods cannot arrive before they were ordered")
    if received_date > date.today() + timedelta(days=1):
        raise SupplyError("the receipt date is in the future")
    await books.open_period(db, received_date)
    if not lines:
        raise SupplyError("nothing received")
    if len({ln.order_line_id for ln in lines}) != len(lines):
        raise SupplyError("an order line appears twice on the receipt")
    if invoice_no is not None and not invoice_no.strip():
        invoice_no = None
    branch = await db.get(Branch, order.branch_id)
    if branch is None or not branch.is_active or branch.is_head_office:
        raise SupplyError("goods are received at an active branch that holds stock")

    order_lines = {ln.line_id: ln for ln in order.lines}
    got = await received_units(db, order.order_id)
    receipt = Receipt(
        receipt_no=await settings.next_doc_no(db, "RCV"),
        order_id=order.order_id, supplier_id=order.supplier_id, branch_id=order.branch_id,
        received_date=received_date, invoice_no=invoice_no, invoice_total=invoice_total,
        notes=notes, total=ZERO, received_by=actor.user_id)
    db.add(receipt)
    await db.flush()

    total = ZERO
    labels_required = await labels.required(db)
    received: list[tuple[ReceiptLine, object]] = []
    to_label: list[tuple[object, tuple[str, ...]]] = []
    for n, ln in enumerate(lines, start=1):
        ol = order_lines.get(ln.order_line_id)
        if ol is None:
            raise SupplyError(f"line {n} is not on order {order.order_no}")
        name = f"{ol.heading} {ol.label or ''}".strip()
        if not (0 < ln.units <= MAX_UNITS) or not (0 <= ln.free_units <= MAX_UNITS):
            raise SupplyError(f"{name}: received units must be above zero")
        left = ol.units - got.get(ol.line_id, 0)
        if ln.units > left:
            raise SupplyError(f"{name}: received {ln.units} but only {left} of {ol.units} "
                              "are still due")
        item = await stock_item_for(db, ol)
        if ln.product_id is not None and ln.product_id != item:
            # Only another pack size of the same Online product (review #3).
            variant = await db.get(OnlineVariant, ln.product_id)
            if (variant is None or not variant.is_active
                    or variant.online_product_id != ol.online_product_id):
                raise SupplyError(f"{name}: that is not a pack size of this product")
            item = ln.product_id
        if item is None:
            raise SupplyError(f"{name}: choose which pack size this is")
        product = await ensure_product(db, item, by_weight=ln.weight_kg is not None)
        if product.kind != "sellable" or not product.is_active:
            raise SupplyError(f"{product.name} is not an active item for sale")
        if product.unit == "kg":
            if ln.weight_kg is None or ln.weight_kg <= 0:
                raise SupplyError(f"{product.name} is kept by weight: enter the kilograms")
            qty = stock.quantity(ln.weight_kg, product)
        else:
            if ln.weight_kg is not None:
                raise SupplyError(f"{product.name} is counted in pieces, not weighed")
            qty = stock.quantity(Decimal(ln.units + ln.free_units), product)
        if labels_required:
            pieces = ln.units + ln.free_units
            if product.unit != "kg" and len(ln.labels) != pieces:
                raise SupplyError(f"{product.name}: {pieces} units need {pieces} QR labels, "
                                  f"{len(ln.labels)} scanned")
            if product.unit == "kg" and not ln.labels:
                raise SupplyError(f"{product.name}: scan the QR label on each bag")
        if ln.mfg_date and ln.expiry_date and ln.mfg_date >= ln.expiry_date:
            raise SupplyError(f"{product.name}: the expiry is not after the manufacture date")
        if ln.mfg_date and ln.mfg_date > received_date:
            raise SupplyError(f"{product.name}: made after it arrived?")
        value = stock.money(Decimal(ln.units) * ol.unit_price)
        batch = await stock.create_batch(
            db, batch_no=f"{receipt.receipt_no}-{n}", branch_id=order.branch_id,
            product=product, supplier_id=order.supplier_id, received_date=received_date,
            expiry_date=ln.expiry_date, qty=qty, value=value)
        row = ReceiptLine(receipt_id=receipt.receipt_id, line_no=n, order_line_id=ol.line_id,
                          product_id=product.product_id, units=ln.units,
                          free_units=ln.free_units, qty=qty, unit_price=ol.unit_price,
                          line_value=value, expiry_date=ln.expiry_date, mfg_date=ln.mfg_date,
                          mrp=ln.mrp, batch_id=batch.batch_id)
        db.add(row)
        got[ol.line_id] = got.get(ol.line_id, 0) + ln.units
        received.append((row, batch))
        if ln.labels:
            to_label.append((batch, ln.labels))
        total += value
    receipt.total = total
    await db.flush()

    journal = None
    if total > 0:
        journal = await ledger.post_journal(
            db, entry_date=received_date, source=SOURCE,
            source_ref=f"receipt:{receipt.receipt_id}",
            memo=f"{receipt.receipt_no} goods received against {order.order_no}"
                 + (f" ({order.finance_po_no})" if order.finance_po_no else ""),
            lines=[ledger.LineIn("1300", order.branch_id, debit=total, memo=receipt.receipt_no),
                   ledger.LineIn("2020", order.branch_id, credit=total, memo=receipt.receipt_no)],
            user_id=actor.user_id)
        receipt.journal_id = journal.journal_id
    for row, batch in received:
        await stock.move(db, batch=batch, move_type="grn", qty=row.qty, value=row.line_value,
                         move_date=received_date, doc_type="receipt", doc_id=receipt.receipt_id,
                         user_id=actor.user_id,
                         journal_id=journal.journal_id if journal else None)

    for batch, numbers in to_label:
        await labels.link(db, actor, batch=batch, numbers=list(numbers))

    left = _outstanding(order, got)
    if left == 0:
        order.status = "received"
    ordered = sum(ln.units for ln in order.lines)
    _emit_receipt(db, order, receipt, received, journal, ordered=ordered, outstanding=left,
                  online_branch_id=branch.online_branch_id)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="receipt.post", entity="receipt",
                       entity_id=receipt.receipt_id,
                       after={"receipt_no": receipt.receipt_no, "order_no": order.order_no,
                              "total": str(total), "outstanding_units": left},
                       ip_address=actor.ip_address)
    await db.refresh(receipt, ["lines"])
    return receipt


def _emit_receipt(db, order, receipt, received, journal, *, ordered: int, outstanding: int,
                  online_branch_id):
    # Finance keys branches by SRIBEESonline's id (review #5)
    branch = {"branch_id": str(order.branch_id),
              "online_branch_id": str(online_branch_id) if online_branch_id else None}
    db.add(OutboxEvent(event="receipt.posted", payload={
        "receipt_id": str(receipt.receipt_id), "receipt_no": receipt.receipt_no,
        "order_id": str(order.order_id), "order_no": order.order_no,
        "finance_po_no": order.finance_po_no, "supplier_id": str(order.supplier_id),
        **branch, "received_date": receipt.received_date.isoformat(),
        "invoice_no": receipt.invoice_no,
        "invoice_total": str(receipt.invoice_total) if receipt.invoice_total is not None
        else None,
        "total": str(receipt.total),
        "journal_event_id": str(journal.journal_id) if journal else None,
        "ordered_units": ordered, "outstanding_units": outstanding,
        "order_status": order.status,
        "lines": [{"order_line_id": str(r.order_line_id), "product_id": str(r.product_id),
                   "units": r.units, "free_units": r.free_units, "qty": str(r.qty),
                   "unit_price": str(r.unit_price), "line_value": str(r.line_value),
                   "batch_no": b.batch_no,
                   "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None}
                  for r, b in received]}))
    db.add(OutboxEvent(event="supplier.notify", payload={
        "order_id": str(order.order_id), "order_no": order.order_no,
        "supplier_id": str(order.supplier_id), "branch_id": str(order.branch_id),
        "status": order.status, "kind": "received",
        "received_units": ordered - outstanding, "ordered_units": ordered}))


async def close_rest(db: AsyncSession, actor: Actor, order_id: uuid.UUID, *, reason: str,
                     scope: set[uuid.UUID] | None = None) -> SupplyOrder:
    """Stop waiting for the rest of a part-received order (decision 31)."""
    if actor.role not in ("owner", "purchasing"):
        raise Forbidden("purchasing or the owner closes a back-order")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise SupplyError("say why the rest will not come")
    order = await _order(db, actor, order_id, scope)
    if order.status not in RECEIVABLE:
        raise Conflict(f"order {order.order_no} is {order.status}")
    got = await received_units(db, order.order_id)
    if not got:
        raise Conflict("nothing was received yet: cancel the order instead")
    left = _outstanding(order, got)
    order.status = "received"
    order.rest_closed_reason, order.rest_closed_at, order.rest_closed_by = (
        reason, _now(), actor.user_id)
    db.add(OutboxEvent(event="order.rest_closed", payload={
        "order_id": str(order.order_id), "order_no": order.order_no,
        "finance_po_no": order.finance_po_no, "supplier_id": str(order.supplier_id),
        "branch_id": str(order.branch_id), "status": order.status,
        "outstanding_units": left, "reason": reason}))
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="order.close_rest", entity="order",
                       entity_id=order.order_id, after={"reason": reason, "units_left": left},
                       ip_address=actor.ip_address)
    return order


async def get_receipt(db: AsyncSession, receipt_id: uuid.UUID,
                      scope: set[uuid.UUID] | None = None) -> Receipt:
    receipt = await db.get(Receipt, receipt_id)
    if receipt is None or (scope is not None and receipt.branch_id not in scope):
        raise NotFound("receipt not found")
    return receipt
