"""Branch transfers, write-offs and stock counts (DESIGN.md §5.4, §6).

    transfer shipped   Dr 1310 In transit (to branch)   Cr 1300 (from branch)
    transfer received  Dr 1300 (to branch)              Cr 1310 (to branch)
    short receipt      Dr 5100 Stock losses              Cr 1310   (once approved)
    write-off          Dr 5100                           Cr 1300   (once approved)
    count variance     Dr/Cr 5100                        Cr/Dr 1300 (once approved)

Every loss uses one approval rule: an accountant approves up to the write-off
limit, the owner above it; nobody approves their own request, and the owner's
own request is approved by raising it (owner, 2026-10-27).
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AdjustmentLine,
    Branch,
    CountLine,
    Product,
    StockAdjustment,
    StockBatch,
    StockCount,
    StockTransfer,
    TransferLine,
)
from app.services import audit, ledger, settings, stock
from app.services.actors import Actor, _now
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

SOURCE = "inventory"
ZERO = Decimal("0")
REASONS = ("expired", "damaged", "theft", "other")


async def _stock_branch(db: AsyncSession, branch_id: uuid.UUID) -> Branch:
    branch = await db.get(Branch, branch_id)
    if branch is None or not branch.is_active or branch.is_head_office:
        raise SupplyError("choose an active branch that holds stock")
    return branch


async def _batches(
    db: AsyncSession, ids: list[uuid.UUID], branch_id: uuid.UUID | None = None
) -> dict[uuid.UUID, StockBatch]:
    if len(set(ids)) != len(ids):
        raise SupplyError("a batch appears twice")
    found = {b.batch_id: b for b in await db.scalars(
        select(StockBatch).where(StockBatch.batch_id.in_(ids)).with_for_update())}
    if len(found) != len(ids):
        raise SupplyError("a batch does not exist")
    branches = {b.branch_id for b in found.values()}
    if len(branches) != 1 or (branch_id is not None and branches != {branch_id}):
        raise SupplyError("all batches must be at the same branch")
    return found


async def _qty(db: AsyncSession, batch: StockBatch, value: Decimal) -> Decimal:
    product = await db.get(Product, batch.product_id)
    return stock.quantity(value, product)


# ---------------------------------------------------------------- adjustments (losses)
def _loss(adj: StockAdjustment) -> Decimal:
    return -adj.value if adj.value < 0 else ZERO


async def _check_loss_approver(db: AsyncSession, actor: Actor, adj: StockAdjustment) -> None:
    if actor.role not in ("owner", "accountant"):
        raise Forbidden("only the owner or the accountant approves stock losses")
    if adj.requested_by == actor.user_id:
        raise Conflict("you cannot approve your own request")
    limit = await settings.get_value(db, settings.WRITEOFF_APPROVAL_LIMIT)
    if actor.role == "accountant" and _loss(adj) > limit:
        raise Forbidden(f"a loss above Rs {limit:,.2f} needs the owner")


async def _new_adjustment(
    db: AsyncSession,
    actor: Actor,
    *,
    branch_id: uuid.UUID,
    kind: str,
    reason: str,
    adjustment_date: date,
    notes: str | None,
    changes: list[tuple[StockBatch, Decimal]],
    value: Decimal | None = None,
    source_id: uuid.UUID | None = None,
) -> StockAdjustment:
    """Record a request. Its value is estimated now and fixed when it posts."""
    if value is None:
        value = ZERO
        for batch, change in changes:
            if change < 0:
                value += await stock.issue_value(db, batch, -change)
            else:
                value += stock.money(change * batch.unit_cost)
    adj = StockAdjustment(
        adjustment_no=await settings.next_doc_no(db, "ADJ"),
        branch_id=branch_id,
        kind=kind,
        reason=reason,
        adjustment_date=adjustment_date,
        status="pending_approval",
        value=value,
        notes=notes,
        requested_by=actor.user_id,
        source_id=source_id,
        lines=[AdjustmentLine(line_no=n, batch_id=b.batch_id, qty_change=c)
               for n, (b, c) in enumerate(changes, start=1)],
    )
    db.add(adj)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action=f"{kind}.request",
                       entity="stock_adjustment", entity_id=adj.adjustment_id,
                       after={"no": adj.adjustment_no, "value": str(value), "reason": reason},
                       ip_address=actor.ip_address)
    if actor.role == "owner":
        await _post_adjustment(db, actor, adj)
    return adj


async def _post_adjustment(db: AsyncSession, actor: Actor, adj: StockAdjustment) -> None:
    b = adj.branch_id
    move_type = {"writeoff": "writeoff", "count": "count"}.get(adj.kind)
    planned: list[tuple[StockBatch, AdjustmentLine, Decimal]] = []
    if adj.kind == "transfer_short":
        total = adj.value
        other = "1310"
    else:
        other = "1300"
        batches = await _batches(db, [ln.batch_id for ln in adj.lines], b)
        total = ZERO
        for ln in adj.lines:
            batch = batches[ln.batch_id]
            if ln.qty_change < 0:
                value = await stock.issue_value(db, batch, -ln.qty_change)
            else:
                value = stock.money(ln.qty_change * batch.unit_cost)
            ln.value = value
            planned.append((batch, ln, value))
            total += value
    journal = None
    if total != 0:
        memo = f"{adj.adjustment_no} {adj.kind.replace('_', ' ')}: {adj.reason}"
        if total < 0:
            lines = [ledger.LineIn("5100", b, debit=-total, memo=adj.adjustment_no),
                     ledger.LineIn(other, b, credit=-total, memo=adj.adjustment_no)]
        else:
            lines = [ledger.LineIn(other, b, debit=total, memo=adj.adjustment_no),
                     ledger.LineIn("5100", b, credit=total, memo=adj.adjustment_no)]
        journal = await ledger.post_journal(
            db, entry_date=adj.adjustment_date, source=SOURCE,
            source_ref=f"adjustment:{adj.adjustment_id}", memo=memo, lines=lines,
            user_id=actor.user_id,
        )
        adj.journal_id = journal.journal_id
    for batch, ln, value in planned:
        await stock.move(db, batch=batch, move_type=move_type, qty=ln.qty_change, value=value,
                         move_date=adj.adjustment_date, doc_type="adjustment",
                         doc_id=adj.adjustment_id, user_id=actor.user_id,
                         journal_id=journal.journal_id if journal else None)
    adj.value = total
    adj.status = "posted"
    adj.approved_by = actor.user_id
    adj.approved_at = _now()
    if adj.kind == "transfer_short":
        transfer = await db.get(StockTransfer, adj.source_id, with_for_update=True)
        transfer.status = "closed"
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action=f"{adj.kind}.post",
                       entity="stock_adjustment", entity_id=adj.adjustment_id,
                       after={"value": str(total)}, ip_address=actor.ip_address)


async def get_adjustment(
    db: AsyncSession, adjustment_id: uuid.UUID, *, lock: bool = False
) -> StockAdjustment:
    adj = await db.get(StockAdjustment, adjustment_id, with_for_update=lock)
    if adj is None:
        raise NotFound("adjustment not found")
    return adj


async def approve_adjustment(
    db: AsyncSession, actor: Actor, adj: StockAdjustment
) -> StockAdjustment:
    if adj.status != "pending_approval":
        raise Conflict(f"{adj.adjustment_no} is not waiting for approval")
    await _check_loss_approver(db, actor, adj)
    await _post_adjustment(db, actor, adj)
    return adj


async def reject_adjustment(
    db: AsyncSession, actor: Actor, adj: StockAdjustment, *, reason: str
) -> StockAdjustment:
    if adj.status != "pending_approval":
        raise Conflict(f"{adj.adjustment_no} is not waiting for approval")
    if adj.kind == "transfer_short":
        raise Conflict("a transit shortage is real; approve it, or find the goods and correct "
                       "it with a new transfer")
    await _check_loss_approver(db, actor, adj)
    adj.status = "rejected"
    adj.rejected_reason = reason
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action=f"{adj.kind}.reject",
                       entity="stock_adjustment", entity_id=adj.adjustment_id,
                       after={"reason": reason}, ip_address=actor.ip_address)
    return adj


@dataclass(frozen=True)
class BatchQtyIn:
    batch_id: uuid.UUID
    qty: Decimal


async def request_writeoff(
    db: AsyncSession,
    actor: Actor,
    *,
    branch_id: uuid.UUID,
    reason: str,
    adjustment_date: date,
    notes: str | None,
    lines: list[BatchQtyIn],
) -> StockAdjustment:
    if reason not in REASONS:
        raise SupplyError(f"reason is one of {', '.join(REASONS)}")
    if not lines:
        raise SupplyError("choose the batches to write off")
    await _stock_branch(db, branch_id)
    batches = await _batches(db, [ln.batch_id for ln in lines], branch_id)
    changes = []
    for ln in lines:
        batch = batches[ln.batch_id]
        qty = await _qty(db, batch, ln.qty)
        if qty <= 0:
            raise SupplyError("quantities must be above zero")
        changes.append((batch, -qty))
    return await _new_adjustment(db, actor, branch_id=branch_id, kind="writeoff", reason=reason,
                                 adjustment_date=adjustment_date, notes=notes, changes=changes)


# ---------------------------------------------------------------- transfers
async def ship_transfer(
    db: AsyncSession,
    actor: Actor,
    *,
    from_branch_id: uuid.UUID,
    to_branch_id: uuid.UUID,
    shipped_date: date,
    notes: str | None,
    lines: list[BatchQtyIn],
) -> StockTransfer:
    if from_branch_id == to_branch_id:
        raise SupplyError("a transfer goes to another branch")
    await _stock_branch(db, from_branch_id)
    await _stock_branch(db, to_branch_id)
    if not lines:
        raise SupplyError("choose the batches to send")
    batches = await _batches(db, [ln.batch_id for ln in lines], from_branch_id)
    transfer = StockTransfer(
        transfer_no=await settings.next_doc_no(db, "TRF"),
        from_branch_id=from_branch_id, to_branch_id=to_branch_id, status="in_transit",
        shipped_date=shipped_date, value_shipped=ZERO, notes=notes, shipped_by=actor.user_id,
    )
    db.add(transfer)
    await db.flush()
    total = ZERO
    planned = []
    for n, ln in enumerate(lines, start=1):
        batch = batches[ln.batch_id]
        qty = await _qty(db, batch, ln.qty)
        if qty <= 0:
            raise SupplyError("quantities must be above zero")
        if batch.status != "available":
            raise SupplyError(f"batch {batch.batch_no} is in quarantine")
        value = await stock.issue_value(db, batch, qty)
        db.add(TransferLine(transfer_id=transfer.transfer_id, line_no=n,
                            from_batch_id=batch.batch_id, qty=qty, value=-value))
        planned.append((batch, qty, value))
        total += -value
    transfer.value_shipped = total
    journal = None
    if total > 0:
        journal = await ledger.post_journal(
            db, entry_date=shipped_date, source=SOURCE,
            source_ref=f"transfer-out:{transfer.transfer_id}",
            memo=f"{transfer.transfer_no} stock sent between branches",
            lines=[ledger.LineIn("1310", to_branch_id, debit=total, memo=transfer.transfer_no),
                   ledger.LineIn("1300", from_branch_id, credit=total, memo=transfer.transfer_no)],
            user_id=actor.user_id,
        )
        transfer.ship_journal_id = journal.journal_id
    for batch, qty, value in planned:
        await stock.move(db, batch=batch, move_type="transfer_out", qty=-qty, value=value,
                         move_date=shipped_date, doc_type="transfer", doc_id=transfer.transfer_id,
                         user_id=actor.user_id, journal_id=journal.journal_id if journal else None)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="transfer.ship", entity="stock_transfer",
                       entity_id=transfer.transfer_id,
                       after={"no": transfer.transfer_no, "value": str(total)},
                       ip_address=actor.ip_address)
    await db.refresh(transfer, ["lines"])
    return transfer


async def get_transfer(
    db: AsyncSession, transfer_id: uuid.UUID, *, lock: bool = False
) -> StockTransfer:
    transfer = await db.get(StockTransfer, transfer_id, with_for_update=lock)
    if transfer is None:
        raise NotFound("transfer not found")
    return transfer


async def receive_transfer(
    db: AsyncSession,
    actor: Actor,
    transfer: StockTransfer,
    *,
    received_date: date,
    received: dict[uuid.UUID, Decimal],
) -> StockTransfer:
    """Each line arrives as a new batch at the receiving branch, keeping its cost,
    expiry and age. Anything missing becomes a stock-loss request."""
    if transfer.status != "in_transit":
        raise Conflict(f"{transfer.transfer_no} is {transfer.status.replace('_', ' ')}")
    if received_date < transfer.shipped_date:
        raise SupplyError("goods cannot arrive before they were sent")
    if set(received) != {ln.transfer_line_id for ln in transfer.lines}:
        raise SupplyError("enter the quantity received for every line (0 if it never came)")

    total = ZERO
    planned = []
    for ln in transfer.lines:
        src = await db.get(StockBatch, ln.from_batch_id)
        qty = await _qty(db, src, received[ln.transfer_line_id])
        if qty < 0 or qty > ln.qty:
            raise SupplyError(f"line {ln.line_no}: received {qty} of {ln.qty} sent")
        ln.qty_received = qty
        if qty == 0:
            continue
        value = ln.value if qty == ln.qty else stock.money(ln.value * qty / ln.qty)
        product = await db.get(Product, src.product_id)
        batch = await stock.create_batch(
            db, batch_no=f"{transfer.transfer_no}-{ln.line_no}", branch_id=transfer.to_branch_id,
            product=product, supplier_id=src.supplier_id, received_date=src.received_date,
            expiry_date=src.expiry_date, qty=qty, value=value,
        )
        ln.to_batch_id = batch.batch_id
        planned.append((batch, qty, value))
        total += value

    transfer.received_date = received_date
    transfer.received_by = actor.user_id
    transfer.value_received = total
    to = transfer.to_branch_id
    journal = None
    if total > 0:
        journal = await ledger.post_journal(
            db, entry_date=received_date, source=SOURCE,
            source_ref=f"transfer-in:{transfer.transfer_id}",
            memo=f"{transfer.transfer_no} stock received from another branch",
            lines=[ledger.LineIn("1300", to, debit=total, memo=transfer.transfer_no),
                   ledger.LineIn("1310", to, credit=total, memo=transfer.transfer_no)],
            user_id=actor.user_id,
        )
        transfer.receive_journal_id = journal.journal_id
    for batch, qty, value in planned:
        await stock.move(db, batch=batch, move_type="transfer_in", qty=qty, value=value,
                         move_date=received_date, doc_type="transfer",
                         doc_id=transfer.transfer_id, user_id=actor.user_id,
                         journal_id=journal.journal_id if journal else None)

    short = transfer.value_shipped - total
    short_qty = any(ln.qty_received < ln.qty for ln in transfer.lines)
    transfer.status = "short_pending" if short_qty else "received"
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="transfer.receive",
                       entity="stock_transfer", entity_id=transfer.transfer_id,
                       after={"value": str(total), "short": str(short)},
                       ip_address=actor.ip_address)
    if short_qty:
        adj = await _new_adjustment(
            db, actor, branch_id=to, kind="transfer_short", reason="transit",
            adjustment_date=received_date, notes=f"Short on {transfer.transfer_no}",
            changes=[], value=-short, source_id=transfer.transfer_id,
        )
        transfer.adjustment_id = adj.adjustment_id
        await db.flush()
    return transfer


# ---------------------------------------------------------------- stock counts
async def start_count(
    db: AsyncSession,
    actor: Actor,
    *,
    branch_id: uuid.UUID,
    count_date: date,
    product_ids: list[uuid.UUID] | None,
    notes: str | None,
) -> StockCount:
    """Freeze what the books say is on the shelf, batch by batch."""
    await _stock_branch(db, branch_id)
    batches = await stock.batches_on_hand(db, branch_ids={branch_id})
    if product_ids:
        wanted = set(product_ids)
        batches = [b for b in batches if b.product_id in wanted]
    if not batches:
        raise SupplyError("there is no stock to count at this branch")
    count = StockCount(
        count_no=await settings.next_doc_no(db, "CNT"), branch_id=branch_id,
        count_date=count_date, status="open", notes=notes, started_by=actor.user_id,
        lines=[CountLine(batch_id=b.batch_id, system_qty=b.qty_on_hand) for b in batches],
    )
    db.add(count)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="count.start", entity="stock_count",
                       entity_id=count.count_id, after={"no": count.count_no,
                                                        "batches": len(batches)},
                       ip_address=actor.ip_address)
    return count


async def get_count(db: AsyncSession, count_id: uuid.UUID, *, lock: bool = False) -> StockCount:
    count = await db.get(StockCount, count_id, with_for_update=lock)
    if count is None:
        raise NotFound("count not found")
    return count


async def enter_counts(
    db: AsyncSession, count: StockCount, counted: dict[uuid.UUID, Decimal]
) -> StockCount:
    if count.status != "open":
        raise Conflict(f"{count.count_no} is {count.status}")
    lines = {ln.batch_id: ln for ln in count.lines}
    for batch_id, qty in counted.items():
        line = lines.get(batch_id)
        if line is None:
            raise SupplyError("a batch is not on this count sheet")
        batch = await db.get(StockBatch, batch_id)
        qty = await _qty(db, batch, qty)
        if qty < 0:
            raise SupplyError("a counted quantity cannot be negative")
        line.counted_qty = qty
    await db.flush()
    return count


async def submit_count(db: AsyncSession, actor: Actor, count: StockCount) -> StockCount:
    """Differences become one count adjustment, approved like any other loss."""
    if count.status != "open":
        raise Conflict(f"{count.count_no} is {count.status}")
    missing = [ln for ln in count.lines if ln.counted_qty is None]
    if missing:
        raise SupplyError(f"{len(missing)} batch(es) are not counted yet")
    diffs = [(ln.batch_id, ln.counted_qty - ln.system_qty) for ln in count.lines
             if ln.counted_qty != ln.system_qty]
    count.status = "submitted"
    count.submitted_by = actor.user_id
    if diffs:
        batches = await _batches(db, [b for b, _ in diffs], count.branch_id)
        adj = await _new_adjustment(
            db, actor, branch_id=count.branch_id, kind="count", reason="count",
            adjustment_date=count.count_date, notes=f"Stock count {count.count_no}",
            changes=[(batches[b], d) for b, d in diffs], source_id=count.count_id,
        )
        count.adjustment_id = adj.adjustment_id
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="count.submit", entity="stock_count",
                       entity_id=count.count_id, after={"differences": len(diffs)},
                       ip_address=actor.ip_address)
    return count


async def cancel_count(db: AsyncSession, actor: Actor, count: StockCount) -> StockCount:
    if count.status != "open":
        raise Conflict(f"{count.count_no} is {count.status}")
    count.status = "cancelled"
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="count.cancel", entity="stock_count",
                       entity_id=count.count_id, ip_address=actor.ip_address)
    return count
