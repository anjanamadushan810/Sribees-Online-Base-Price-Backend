"""Product correction (owner 2026-11-26): goods received under the wrong product (a
duplicate, or the wrong kind of a shampoo) move to the right one. The units leave the
batch at its cost and a new batch of the right product takes them at the same value,
keeping the supplier, receipt date and expiry. Value stays in 1300 of the same branch, so
there is no journal. A received batch is never edited (its identity is fixed); this is
the audited way to put it right. Owner only."""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, StockBatch
from app.services import audit, books, settings, stock
from app.services.actors import Actor
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError
from app.services.picks import colombo_today
from app.services.receiving import ensure_product


async def correct(db: AsyncSession, actor: Actor, *, batch_id: uuid.UUID, qty: Decimal,
                  to_product_id: uuid.UUID, reason: str) -> dict:
    if actor.role != "owner":
        raise Forbidden("only the owner corrects the product of received stock")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise SupplyError("say why the product is wrong")
    source = await db.get(StockBatch, batch_id, with_for_update=True)
    if source is None:
        raise NotFound("batch not found")
    if source.status != "available":
        raise Conflict(f"batch {source.batch_no} is in {source.status}")
    if to_product_id == source.product_id:
        raise SupplyError("the batch is already on that product")
    old = await db.get(Product, source.product_id)
    new = await ensure_product(db, to_product_id)
    if old.unit != new.unit:
        raise SupplyError("both products must be counted the same way")
    qty = stock.quantity(qty, old)
    if qty <= 0:
        raise SupplyError("move at least one unit")
    if qty > source.qty_on_hand - await stock.reserved(db, source.batch_id):
        raise SupplyError(f"batch {source.batch_no} has only {source.qty_on_hand} free")

    value_out = await stock.issue_value(db, source, qty)  # negative, at the batch's cost
    doc_id = uuid.uuid4()
    no = await settings.next_doc_no(db, "COR")
    today = colombo_today()
    await books.open_period(db, today)
    await stock.move(db, batch=source, move_type="correct_out", qty=-qty, value=value_out,
                     move_date=today, doc_type="correction", doc_id=doc_id,
                     user_id=actor.user_id)
    batch = await stock.create_batch(
        db, batch_no=f"{no}-1", branch_id=source.branch_id, product=new,
        supplier_id=source.supplier_id, received_date=source.received_date,
        expiry_date=source.expiry_date, qty=qty, value=-value_out)
    await stock.move(db, batch=batch, move_type="correct_in", qty=qty, value=-value_out,
                     move_date=today, doc_type="correction", doc_id=doc_id,
                     user_id=actor.user_id)
    await audit.record(db, user_id=actor.user_id, action="stock.correct_product",
                       entity="stock_batch", entity_id=source.batch_id,
                       after={"correction_no": no, "qty": str(qty), "value": str(-value_out),
                              "from_product": str(old.product_id), "from_name": old.name,
                              "to_product": str(new.product_id), "to_name": new.name,
                              "new_batch": batch.batch_no, "reason": reason},
                       ip_address=actor.ip_address)
    return {"correction_no": no, "batch_id": batch.batch_id, "batch_no": batch.batch_no,
            "qty": qty, "value": -value_out}
