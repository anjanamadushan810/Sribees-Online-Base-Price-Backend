"""Receiving goods against a supply order (joint-go-live plan, phase B; replaces Finance's
GRN, which this follows line for line).

    receipt   Dr 1300 Inventory (branch)   Cr 2020 Goods received not invoiced

Each received line becomes a batch at its landed cost (paid value plus its share of the
transport charge, over paid + free quantity). The transport charge staff read off the
bill is split over the received lines by weight (app/services/transport.py), and the base
price is that delivered price. A receipt closes the order, also when it is short (owner
2026-11-26, replaces decision 31): what did not come is ordered again. The supplier's
invoice number and total are entered here (decision 23); Finance matches them three ways
from the `receipt.posted` event.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    BasePrice,
    Branch,
    Label,
    OnlineProduct,
    OnlineStockSend,
    OnlineVariant,
    OutboxEvent,
    Product,
    Receipt,
    ReceiptDraftLabel,
    ReceiptLine,
    Supplier,
    SupplyOrder,
    SupplyOrderLine,
)
from app.services import audit, books, labels, ledger, settings, stock, transport
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
    order_line_id: uuid.UUID | None  # None: a product that came without being ordered
    units: int
    free_units: int = 0
    weight_kg: Decimal | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = None
    product_id: uuid.UUID | None = None
    labels: tuple[str, ...] = ()  # QR label numbers stuck on these units (phase C)
    # the bill's price per unit (owner 2026-11-26): staff enter it at receiving; the order's
    # price is the default. It is the item's base price.
    unit_price: Decimal | None = None
    listed_price: Decimal | None = None  # the shop's prices, when staff change them
    selling_price: Decimal | None = None
    # owner 2026-11-26: goods that came without being ordered are added at receiving, but
    # only products we already sell on Online (a new product is made by the super admin)
    online_variant_id: uuid.UUID | None = None
    # owner 2026-11-27: kept aside WITHOUT QR labels (busy days); in stock and Finance now,
    # on Online only when labels are stuck on later (app/services/held.py)
    hold: bool = False


async def _unordered(db: AsyncSession, order: SupplyOrder, ln: ReceiptLineIn, n: int, *,
                     write: bool) -> tuple[ReceiptLineIn, SupplyOrderLine]:
    """The order line for an item that was not ordered: checked, and added to the order
    (marked added_at_receiving) when `write`."""
    if ln.online_variant_id is None:
        raise SupplyError(f"line {n}: name the order line, or the product that came")
    if ln.unit_price is None:
        raise SupplyError(f"line {n}: enter the bill price of an item that was not ordered")
    variant = await db.get(OnlineVariant, ln.online_variant_id)
    product = await db.get(OnlineProduct, variant.online_product_id) if variant else None
    if variant is None or not variant.is_active or product is None or not product.is_active:
        raise SupplyError(f"line {n}: that product is not sold on SRIBEES Online; the super "
                          "admin adds new products there first")
    if any(x.online_variant_id == ln.online_variant_id for x in order.lines):
        raise SupplyError(f"{product.name} is already on the order: receive it on its line")
    label = (variant.name if variant.name and variant.name.lower() not in product.name.lower()
             else "")
    ol = SupplyOrderLine(
        order_id=order.order_id, supplier_id=order.supplier_id, offer_id=None,
        online_product_id=product.online_product_id, online_variant_id=variant.online_variant_id,
        heading=product.name[:200], label=label[:120], boxes=ln.units, units_per_box=1,
        unit_price=ln.unit_price, mrp=ln.mrp, added_at_receiving=True)
    if write:
        db.add(ol)
        await db.flush()
        await db.refresh(ol, ["units", "line_total"])
    else:  # a preview: the line answers under the product's pack size id
        ol.line_id = variant.online_variant_id
        ol.units = ln.units
    return replace(ln, order_line_id=ol.line_id), ol


@dataclass
class _Checked:
    """A received line that passed its checks, before anything is written for it."""

    line: ReceiptLineIn
    order_line: SupplyOrderLine
    name: str
    item: uuid.UUID
    product: Product
    qty: Decimal
    price: Decimal  # per unit, as billed
    value: Decimal
    weight_grams: int | None  # of everything that arrived on this line




# ---------------------------------------------------------------- stock products
async def stock_item_for(db: AsyncSession, line: SupplyOrderLine) -> uuid.UUID | None:
    """The stock product of an order line: its pack size (Online variant), or the only
    pack size of its product. Same rule as the finance_product_id on order.submitted."""
    if line.online_variant_id is not None:
        return line.online_variant_id
    if line.online_product_id is None:
        return None
    ids = list(await db.scalars(select(OnlineVariant.online_variant_id).where(
        OnlineVariant.online_product_id == line.online_product_id,
        # a switched-off default pack size is not a real one (online_catalog)
        (OnlineVariant.online_variant_id != line.online_product_id) | OnlineVariant.is_active)))
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


# ---------------------------------------------------------------- weight and transport
async def unit_weight_grams(db: AsyncSession, variant_id: uuid.UUID) -> int | None:
    """What one unit of a pack size weighs: the weight option's own grams, else the weight
    entered on the Online product (the one delivery uses; owner 2026-11-26). Never guessed
    from the size printed in the name."""
    variant = await db.get(OnlineVariant, variant_id)
    if variant is None:
        return None
    if variant.online_variant_id != variant.online_product_id and variant.weight_grams:
        return variant.weight_grams
    parent = await db.get(OnlineProduct, variant.online_product_id)
    return parent.weight_grams if parent else None


async def charge_due(db: AsyncSession, order: SupplyOrder) -> Decimal:
    """What is still to be carried of the charge the supplier typed in their app: the
    figure offered when staff enter none."""
    carried = await db.scalar(select(func.coalesce(func.sum(Receipt.transport_charge), 0))
                              .where(Receipt.order_id == order.order_id))
    landed = await db.scalar(
        select(func.coalesce(func.sum(ReceiptLine.landed_cost), 0))
        .join(Receipt, Receipt.receipt_id == ReceiptLine.receipt_id)
        .where(Receipt.order_id == order.order_id))
    return max(ZERO, (order.delivery_charge or ZERO) - max(carried or ZERO, landed or ZERO))


def _transport(actor: Actor, charge: Decimal, checked: list[_Checked]) -> list[Decimal]:
    shares = transport.split_by_weight(
        charge, [transport.WeighedLine(c.name, c.weight_grams) for c in checked])
    goods = sum((c.value for c in checked), ZERO)
    cap = stock.money(goods * get_settings().delivery_charge_max_pct / 100)
    if charge > cap and actor.role != "owner":
        raise Forbidden(f"a transport charge above Rs {cap:,.2f} for these goods is saved "
                        "by the owner")
    return shares


async def preview(db: AsyncSession, actor: Actor, order_id: uuid.UUID, *,
                  lines: list[ReceiptLineIn], transport_charge: Decimal | None,
                  scope: set[uuid.UUID] | None = None) -> dict:
    """What saving would make of the transport charge: each line's share and delivered
    unit price. Writes nothing; lines without a weight are listed instead of guessed."""
    order = await _order(db, actor, order_id, scope)
    order_lines = {ln.line_id: ln for ln in order.lines}
    charge = (await charge_due(db, order) if transport_charge is None
              else stock.money(transport_charge))
    rows = []
    for n, ln in enumerate(lines, start=1):
        if ln.order_line_id is None:
            ln, extra = await _unordered(db, order, ln, n, write=False)
            order_lines[extra.line_id] = extra
        ol = order_lines.get(ln.order_line_id)
        if ol is None or ln.units <= 0:
            raise SupplyError("every line names an order line and units above zero")
        item = ln.product_id or await stock_item_for(db, ol)
        if ln.weight_kg is not None:
            grams = int(ln.weight_kg * 1000)
        else:
            each = await unit_weight_grams(db, item) if item else None
            grams = each * (ln.units + ln.free_units) if each else None
        price = ol.unit_price if ln.unit_price is None else ln.unit_price
        rows.append({"order_line_id": ol.line_id, "name": f"{ol.heading} {ol.label}".strip(),
                     "units": ln.units, "unit_price": price,
                     "value": stock.money(Decimal(ln.units) * price), "weight_grams": grams,
                     "transport": None, "delivered_unit_price": None})
    missing = [r["name"] for r in rows if charge > 0 and not r["weight_grams"]]
    if rows and not missing:
        shares = transport.split_by_weight(
            charge, [transport.WeighedLine(r["name"], r["weight_grams"]) for r in rows])
        for row, share in zip(rows, shares, strict=True):
            row["transport"] = share
            row["delivered_unit_price"] = transport.delivered_unit_price(
                row["unit_price"], share, row["units"])
    goods = sum((r["value"] for r in rows), ZERO)
    cap = stock.money(goods * get_settings().delivery_charge_max_pct / 100)
    return {"transport_charge": charge, "goods_total": goods, "total": goods + charge,
            "transport_cap": cap, "over_cap": charge > cap, "missing_weight": missing,
            "lines": rows}


async def form(db: AsyncSession, actor: Actor, order_id: uuid.UUID,
               scope: set[uuid.UUID] | None = None) -> dict:
    """What the receiving page starts from: what is still due on each line, the order's
    price, the unit weight, and the supplier's transport figure."""
    order = await _order(db, actor, order_id, scope)
    got = await received_units(db, order.order_id)
    rows = []
    for ol in order.lines:
        due = ol.units - got.get(ol.line_id, 0)
        if due <= 0:
            continue
        item = await stock_item_for(db, ol)
        sizes = []
        if item is None and ol.online_product_id is not None:
            sizes = [{"online_variant_id": v.online_variant_id, "name": v.name}
                     for v in await db.scalars(select(OnlineVariant).where(
                         OnlineVariant.online_product_id == ol.online_product_id,
                         OnlineVariant.is_active.is_(True)).order_by(OnlineVariant.name))]
        product = await db.get(Product, item) if item else None
        parent = (await db.get(OnlineProduct, ol.online_product_id)
                  if ol.online_product_id else None)
        rows.append({
            "order_line_id": ol.line_id, "name": f"{ol.heading} {ol.label}".strip(),
            "name_si": parent.name_si if parent else None,
            "image_url": parent.image_url if parent else None,
            "online_product_id": ol.online_product_id, "online_variant_id": item,
            "pack_sizes": sizes, "due_units": due, "unit_price": ol.unit_price, "mrp": ol.mrp,
            "by_weight": bool(product and product.unit == "kg"),
            "unit_weight_grams": await unit_weight_grams(db, item) if item else None})
    supplier = await db.get(Supplier, order.supplier_id)
    branch = await db.get(Branch, order.branch_id)
    return {"order_id": order.order_id, "order_no": order.order_no, "status": order.status,
            "supplier_name": supplier.name, "branch_name": branch.name,
            "online_branch_id": branch.online_branch_id,
            "transport_charge": await charge_due(db, order),
            "labels_required": await labels.required(db), "lines": rows}


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
    order = await db.get(SupplyOrder, order_id, with_for_update=True, populate_existing=True)
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
    transport_charge: Decimal | None = None,
    scope: set[uuid.UUID] | None = None,
    split_lines: bool = False,
    draft_id: uuid.UUID | None = None,
    pass_through: bool = False,
) -> Receipt:
    """split_lines (a staff-app draft, plan 2026-11-27): one order line may come on several
    receipt lines (one box with two expiry dates = two batches); their units together stay
    within what is due. draft_id: the draft being finished, whose reserved QR labels are
    this receipt's own; a label reserved by any other draft is refused.
    pass_through: only the fulfilment partner's day order (plan P4): its held goods leave at
    once to customer orders, so a line without an expiry is taken (one with an expiry is
    still checked) and weighed goods may be held too."""
    order = await _order(db, actor, order_id, scope)
    supplier = await db.get(Supplier, order.supplier_id)
    quick = order.is_quick_buy and order.status == "submitted"  # goods already in hand
    if order.status not in RECEIVABLE and not quick:
        raise Conflict(f"order {order.order_no} is {order.status}; goods are received against "
                       "an order the supplier accepted")
    if (order.status == "approved" and not order.is_quick_buy
            and any(u.is_active for u in supplier.users)):
        raise Conflict(f"{supplier.name} has not accepted order {order.order_no} yet")
    submitted = order.submitted_at.date() if order.submitted_at else None
    # a quick buy is written up after the market bill (owner 2026-11-26: bills of the last
    # days): the bill's date stands; the open-period check below still bounds it
    if submitted and not order.is_quick_buy and received_date < submitted - timedelta(days=1):
        raise SupplyError("goods cannot arrive before they were ordered")
    if received_date > date.today() + timedelta(days=1):
        raise SupplyError("the receipt date is in the future")
    await books.open_period(db, received_date)
    if not lines:
        raise SupplyError("nothing received")
    named = [ln.order_line_id for ln in lines if ln.order_line_id is not None]
    extra = [ln.online_variant_id for ln in lines if ln.order_line_id is None]
    if not split_lines and (len(set(named)) != len(named) or len(set(extra)) != len(extra)):
        raise SupplyError("an order line appears twice on the receipt")
    await _labels_free_of_drafts(db, lines, draft_id)
    if invoice_no is not None and not invoice_no.strip():
        invoice_no = None
    branch = await db.get(Branch, order.branch_id)
    if branch is None or not branch.is_active or branch.is_head_office:
        raise SupplyError("goods are received at an active branch that holds stock")

    added = []
    extra_line: dict[uuid.UUID, uuid.UUID] = {}  # a product that came unordered -> its new line
    for n, ln in enumerate(lines, start=1):
        if ln.order_line_id is None and ln.online_variant_id in extra_line:
            ln = replace(ln, order_line_id=extra_line[ln.online_variant_id])
        elif ln.order_line_id is None:
            total = sum(x.units for x in lines if x.order_line_id is None
                        and x.online_variant_id == ln.online_variant_id)
            ln, _ = await _unordered(db, order, replace(ln, units=total), n, write=True)
            ln = replace(ln, units=lines[n - 1].units)
            extra_line[lines[n - 1].online_variant_id] = ln.order_line_id
        added.append(ln)
    lines = added
    await db.refresh(order, ["lines"])
    order_lines = {ln.line_id: ln for ln in order.lines}
    got = await received_units(db, order.order_id)
    charge = (await charge_due(db, order) if transport_charge is None
              else stock.money(transport_charge))
    labels_required = await labels.required(db)

    checked: list[_Checked] = []
    running = dict(got)  # split lines: each one sees what the lines before it took
    for n, ln in enumerate(lines, start=1):
        checked.append(await _check_line(db, order, order_lines, running, ln, n, received_date,
                                         labels_required, pass_through=pass_through))
        running[ln.order_line_id] = running.get(ln.order_line_id, 0) + ln.units
    shares = _transport(actor, charge, checked)

    receipt = Receipt(
        receipt_no=await settings.next_doc_no(db, "RCV"),
        order_id=order.order_id, supplier_id=order.supplier_id, branch_id=order.branch_id,
        received_date=received_date, invoice_no=invoice_no, invoice_total=invoice_total,
        notes=notes, total=ZERO, transport_charge=charge, received_by=actor.user_id)
    db.add(receipt)
    await db.flush()

    total = ZERO
    received: list[tuple[ReceiptLine, object]] = []
    to_label: list[tuple[object, tuple[str, ...]]] = []
    for n, (c, landed) in enumerate(zip(checked, shares, strict=True), start=1):
        ln, ol = c.line, c.order_line
        batch = await stock.create_batch(
            db, batch_no=f"{receipt.receipt_no}-{n}", branch_id=order.branch_id,
            product=c.product, supplier_id=order.supplier_id, received_date=received_date,
            expiry_date=ln.expiry_date, qty=c.qty, value=c.value + landed)
        row = ReceiptLine(receipt_id=receipt.receipt_id, line_no=n, order_line_id=ol.line_id,
                          product_id=c.product.product_id, units=ln.units,
                          free_units=ln.free_units, qty=c.qty, unit_price=c.price,
                          line_value=c.value, expiry_date=ln.expiry_date, mfg_date=ln.mfg_date,
                          mrp=ln.mrp, batch_id=batch.batch_id, landed_cost=landed,
                          listed_price=ln.listed_price, selling_price=ln.selling_price,
                          hold=ln.hold)
        db.add(row)
        got[ol.line_id] = got.get(ol.line_id, 0) + ln.units
        received.append((row, batch))
        await _base_price(db, order, ol, c.item, c.price,
                          transport.delivered_unit_price(c.price, landed, ln.units),
                          ln.mrp or ol.mrp)
        if ln.labels:
            to_label.append((batch, ln.labels))
        total += c.value + landed
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
        await stock.move(db, batch=batch, move_type="grn", qty=row.qty,
                         value=row.line_value + row.landed_cost,
                         move_date=received_date, doc_type="receipt", doc_id=receipt.receipt_id,
                         user_id=actor.user_id,
                         journal_id=journal.journal_id if journal else None)

    for batch, numbers in to_label:
        await labels.link(db, actor, batch=batch, numbers=list(numbers))

    left = _outstanding(order, got)
    order.status = "received"  # a short delivery closes the order too (owner 2026-11-26)
    if left:
        order.rest_closed_reason = "short delivery"
        order.rest_closed_at, order.rest_closed_by = _now(), actor.user_id
    if get_settings().online_stock_in:
        await db.flush()
        for row, _ in received:
            if row.hold:  # goes to Online when its labels are stuck on (held.release)
                continue
            db.add(OnlineStockSend(receipt_line_id=row.receipt_line_id,
                                   receipt_id=receipt.receipt_id))
    ordered = sum(ln.units for ln in order.lines)
    _emit_receipt(db, order, receipt, received, journal, ordered=ordered, outstanding=left,
                  online_branch_id=branch.online_branch_id)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="receipt.post", entity="receipt",
                       entity_id=receipt.receipt_id,
                       after={"receipt_no": receipt.receipt_no, "order_no": order.order_no,
                              "total": str(total), "transport_charge": str(charge),
                              "outstanding_units": left},
                       ip_address=actor.ip_address)
    await db.refresh(receipt, ["lines"])
    return receipt


async def _labels_free_of_drafts(db: AsyncSession, lines: list[ReceiptLineIn],
                                 draft_id: uuid.UUID | None) -> None:
    """A QR label reserved by a staff-app draft belongs to that draft (plan 2026-11-27)."""
    numbers = sorted({labels.parse_number(n) for ln in lines for n in ln.labels})
    if not numbers:
        return
    # the same lock a phone's scan takes: a hold cannot slip in between this check and linking
    await db.execute(select(Label.number).where(Label.number.in_(numbers))
                     .order_by(Label.number).with_for_update())
    q = select(ReceiptDraftLabel.number).where(ReceiptDraftLabel.number.in_(numbers))
    if draft_id is not None:
        q = q.where(ReceiptDraftLabel.draft_id != draft_id)
    taken = (await db.scalars(q.limit(1))).first()
    if taken:
        raise Conflict(f"label {taken} is held by a delivery being received in the staff app")


async def _check_line(db: AsyncSession, order: SupplyOrder, order_lines: dict, got: dict,
                      ln: ReceiptLineIn, n: int, received_date: date,
                      labels_required: bool, *, pass_through: bool = False) -> _Checked:
    ol = order_lines.get(ln.order_line_id)
    if ol is None:
        raise SupplyError(f"line {n} is not on order {order.order_no}")
    name = f"{ol.heading} {ol.label or ''}".strip()
    if not (0 < ln.units <= MAX_UNITS) or not (0 <= ln.free_units <= MAX_UNITS):
        raise SupplyError(f"{name}: received units must be above zero")
    # owner 2026-11-27: more than was ordered is taken (a supplier sends a different
    # quantity for many reasons); staff confirm it in the app, the bill total shows it
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
        grams = int(qty * 1000)
    else:
        if ln.weight_kg is not None:
            raise SupplyError(f"{product.name} is counted in pieces, not weighed")
        qty = stock.quantity(Decimal(ln.units + ln.free_units), product)
        each = await unit_weight_grams(db, item)
        grams = each * (ln.units + ln.free_units) if each else None
    if ln.hold:
        if product.unit == "kg" and not pass_through:
            raise SupplyError(f"{product.name} is kept by weight: weighed goods cannot be held "
                              "without labels yet")
        if ln.labels:
            raise SupplyError(f"{product.name}: a part held without QR takes no labels")
    elif labels_required:
        pieces = ln.units + ln.free_units
        if product.unit != "kg" and len(ln.labels) != pieces:
            raise SupplyError(f"{product.name}: {pieces} units need {pieces} QR labels, "
                              f"{len(ln.labels)} scanned")
        if product.unit == "kg" and not ln.labels:
            raise SupplyError(f"{product.name}: scan the QR label on each bag")
    from app.services.drafts import check_expiry  # noqa: PLC0415 -- drafts imports us
    if not (pass_through and ln.expiry_date is None):
        check_expiry(ln.expiry_date, received_date, ln.mfg_date, product.name)
    if ln.mfg_date and ln.expiry_date and ln.mfg_date >= ln.expiry_date:
        raise SupplyError(f"{product.name}: the expiry is not after the manufacture date")
    if ln.mfg_date and ln.mfg_date > received_date:
        raise SupplyError(f"{product.name}: made after it arrived?")
    price = ol.unit_price if ln.unit_price is None else ln.unit_price
    if price < 0:
        raise SupplyError(f"{product.name}: the price cannot be below zero")
    if (ln.listed_price is not None and ln.selling_price is not None
            and ln.selling_price > ln.listed_price):
        raise SupplyError(f"{product.name}: the selling price is above the listed price")
    return _Checked(line=ln, order_line=ol, name=name, item=item, product=product, qty=qty,
                    price=price, value=stock.money(Decimal(ln.units) * price),
                    weight_grams=grams)


async def _base_price(db: AsyncSession, order: SupplyOrder, ol: SupplyOrderLine,
                      variant_id: uuid.UUID, bill_price: Decimal, delivered: Decimal,
                      mrp: Decimal | None) -> None:
    """Base price = what a unit cost us delivered to the store: the bill price plus its
    share of the transport (owner 2026-11-26). The bill price is kept beside it."""
    variant = await db.get(OnlineVariant, variant_id)
    if variant is None:
        return
    db.add(BasePrice(online_product_id=variant.online_product_id, online_variant_id=variant_id,
                     supplier_id=order.supplier_id, order_id=order.order_id,
                     line_id=ol.line_id, unit_price=delivered, bill_price=bill_price, mrp=mrp))


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
                   "landed_cost": str(r.landed_cost),
                   "batch_no": b.batch_no,
                   "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None}
                  for r, b in received]}))
    if not order.is_quick_buy:  # paid at their counter: nothing to tell their app
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
