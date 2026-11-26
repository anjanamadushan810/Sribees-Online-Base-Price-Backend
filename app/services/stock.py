"""Batches and stock moves (DESIGN.md §6).

A batch is one product line on one receipt, with its own landed cost and
expiry. Its quantity and value change only through stock moves; a database
trigger applies each move to the batch (alembic/versions/0002). Every move
carries an exact rupee value, so the inventory subledger always equals the
sum of its moves, and an issue that empties a batch takes its last cent.
"""

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, StockBatch, StockMove
from app.services.errors import SupplyError

CENT = Decimal("0.01")
MILLI = Decimal("0.001")
COST = Decimal("0.0001")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def quantity(value: Decimal, product: Product) -> Decimal:
    """A quantity in the product's unit: whole numbers for 'each', grams for 'kg'."""
    value = Decimal(value)
    if value != value.quantize(MILLI):
        raise SupplyError(f"{product.name}: quantity {value} has more than three decimals")
    if product.unit == "each" and value != value.to_integral_value():
        raise SupplyError(f"{product.name} is counted in whole units, not {value}")
    return value.quantize(MILLI)


async def create_batch(
    db: AsyncSession,
    *,
    batch_no: str,
    branch_id: uuid.UUID,
    product: Product,
    supplier_id: uuid.UUID | None,
    received_date: date,
    expiry_date: date | None,
    qty: Decimal,
    value: Decimal,
) -> StockBatch:
    """A new, empty batch whose unit cost is value / qty. The caller then moves the stock in."""
    if product.track_expiry and expiry_date is None:
        raise SupplyError(f"{product.name} needs an expiry date")
    if expiry_date is not None and expiry_date <= received_date:
        raise SupplyError(f"{product.name}: expiry {expiry_date} is not after the receipt date")
    batch = StockBatch(
        batch_no=batch_no,
        branch_id=branch_id,
        product_id=product.product_id,
        supplier_id=supplier_id,
        received_date=received_date,
        expiry_date=expiry_date,
        unit_cost=(value / qty).quantize(COST, rounding=ROUND_HALF_UP),
        qty_received=qty,
    )
    db.add(batch)
    await db.flush()
    return batch


async def reserved(db: AsyncSession, batch_id) -> Decimal:
    """Units held for Online orders being packed (pack scan picks, phase E)."""
    from sqlalchemy import func

    from app.models import Pick

    return await db.scalar(select(func.coalesce(func.sum(Pick.qty), 0)).where(
        Pick.batch_id == batch_id, Pick.status == "picked")) or Decimal("0")


async def issue_value(db: AsyncSession, batch: StockBatch, qty: Decimal, *,
                      for_picks: bool = False) -> Decimal:
    """The (negative) value of taking qty out of the batch now: its average remaining
    value, or all of it when the batch empties. Locks the batch row. Units held for Online
    orders (picks) cannot be written off, counted away or transferred; only their own
    delivery (for_picks) takes them."""
    await db.refresh(batch, ["qty_on_hand", "value_on_hand"], with_for_update=True)
    if qty > batch.qty_on_hand:
        raise SupplyError(f"batch {batch.batch_no} holds {batch.qty_on_hand}, cannot issue {qty}")
    if not for_picks:
        held = await reserved(db, batch.batch_id)
        if held and qty > batch.qty_on_hand - held:
            raise SupplyError(f"batch {batch.batch_no}: {held} of {batch.qty_on_hand} are held "
                              "for Online orders being packed; take those scans back first")
    if qty == batch.qty_on_hand:
        return -batch.value_on_hand
    return -money(batch.value_on_hand * qty / batch.qty_on_hand)


async def move(
    db: AsyncSession,
    *,
    batch: StockBatch,
    move_type: str,
    qty: Decimal,
    move_date: date,
    doc_type: str,
    doc_id: uuid.UUID,
    user_id: uuid.UUID | None,
    value: Decimal | None = None,
    journal_id: uuid.UUID | None = None,
) -> StockMove:
    """Record one move. For an issue (qty < 0) leave value out: it is taken from the
    batch at its average remaining value, and all of it when the batch empties."""
    if value is None:
        if qty > 0:
            raise SupplyError("a receipt into a batch needs its value")
        value = await issue_value(db, batch, -qty)
    row = StockMove(
        batch_id=batch.batch_id,
        move_date=move_date,
        move_type=move_type,
        qty=qty,
        value=value,
        doc_type=doc_type,
        doc_id=doc_id,
        journal_id=journal_id,
        created_by=user_id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(batch, ["qty_on_hand", "value_on_hand"])
    return row


async def batches_on_hand(
    db: AsyncSession,
    *,
    branch_ids: set[uuid.UUID] | None,
    product_id: uuid.UUID | None = None,
    expiring_before: date | None = None,
) -> list[StockBatch]:
    """Batches with stock, in picking order: first expiry first, then oldest receipt."""
    q = select(StockBatch).where(StockBatch.qty_on_hand > 0)
    if branch_ids is not None:
        q = q.where(StockBatch.branch_id.in_(branch_ids))
    if product_id is not None:
        q = q.where(StockBatch.product_id == product_id)
    if expiring_before is not None:
        q = q.where(StockBatch.expiry_date <= expiring_before)
    q = q.order_by(
        StockBatch.product_id,
        StockBatch.expiry_date.asc().nulls_last(),
        StockBatch.received_date,
        StockBatch.batch_no,
    )
    return list(await db.scalars(q))
