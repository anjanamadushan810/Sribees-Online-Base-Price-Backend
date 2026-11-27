"""Held stock (owner 2026-11-27): on busy days part of a delivery is checked (count, price,
expiry) and kept aside WITHOUT QR labels.

- It is in stock and in Finance from the day it arrived (the receipt line has ``hold``).
- It is not on SRIBEES Online: Online orders are packed by scanning a QR label.
- Later staff stick QR labels on some of it (a release, any number at a time): those units
  go into the shop, and the worker sends them to Online's stock (keyed by the release id, so
  a resend is safe). The rest stays held.
- A held batch takes labels only through a release (labels.link refuses it otherwise), so
  Online never misses units that got labels.

Goods kept by weight cannot be held yet (one label per bag needs its weight).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    Branch,
    OnlineProduct,
    OnlineVariant,
    PriceReview,
    Product,
    Receipt,
    ReceiptLine,
    StockBatch,
    StockRelease,
)
from app.services import audit, labels, online_stock, price_reviews
from app.services.actors import Actor
from app.services.drafts import colombo_today
from app.services.errors import Conflict, NotFound, SupplyError

ROUND = 50  # releases sent per worker round


async def _released(db: AsyncSession, line_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not line_ids:
        return {}
    rows = await db.execute(
        select(StockRelease.receipt_line_id, func.sum(StockRelease.units))
        .where(StockRelease.receipt_line_id.in_(line_ids))
        .group_by(StockRelease.receipt_line_id))
    return {k: int(v) for k, v in rows}


def _left(line: ReceiptLine, batch: StockBatch, released: int) -> int:
    """Held units still without labels; never more than the batch still holds (a write-off
    of held goods lowers it)."""
    return max(0, min(line.units + line.free_units - released, int(batch.qty_on_hand)))


async def held_lines(db: AsyncSession, scope: set[uuid.UUID] | None) -> list[dict]:
    q = (select(ReceiptLine, StockBatch, Receipt, Product, OnlineProduct, Branch)
         .join(StockBatch, StockBatch.batch_id == ReceiptLine.batch_id)
         .join(Receipt, Receipt.receipt_id == ReceiptLine.receipt_id)
         .join(Product, Product.product_id == ReceiptLine.product_id)
         .outerjoin(OnlineVariant, OnlineVariant.online_variant_id == ReceiptLine.product_id)
         .outerjoin(OnlineProduct,
                    OnlineProduct.online_product_id == OnlineVariant.online_product_id)
         .join(Branch, Branch.branch_id == Receipt.branch_id)
         .where(ReceiptLine.hold.is_(True), StockBatch.qty_on_hand > 0,
                StockBatch.status == "available")
         .order_by(Receipt.received_date, Product.name))
    if scope is not None:
        q = q.where(Receipt.branch_id.in_(scope))
    rows = (await db.execute(q)).all()
    released = await _released(db, [r[0].receipt_line_id for r in rows])
    today = colombo_today()
    out = []
    for line, batch, receipt, product, op, branch in rows:
        left = _left(line, batch, released.get(line.receipt_line_id, 0))
        if left <= 0:
            continue
        out.append({
            "receipt_line_id": line.receipt_line_id, "batch_id": batch.batch_id,
            "batch_no": batch.batch_no, "receipt_no": receipt.receipt_no,
            "branch_id": branch.branch_id, "branch_name": branch.name,
            "product_id": product.product_id, "name": product.name,
            "name_si": op.name_si if op else None, "image_url": op.image_url if op else None,
            "received_date": receipt.received_date, "expiry_date": batch.expiry_date,
            "mrp": line.mrp, "held": line.units + line.free_units,
            "released": released.get(line.receipt_line_id, 0), "left": left,
            "days": (today - receipt.received_date).days})
    return out


async def summary(db: AsyncSession, scope: set[uuid.UUID] | None) -> dict:
    rows = await held_lines(db, scope)
    return {"lines": len(rows), "units": sum(r["left"] for r in rows),
            "oldest_days": max((r["days"] for r in rows), default=0)}


async def is_held(db: AsyncSession, batch_id: uuid.UUID) -> bool:
    return bool(await db.scalar(select(func.count()).select_from(ReceiptLine).where(
        ReceiptLine.batch_id == batch_id, ReceiptLine.hold.is_(True))))


async def release(db: AsyncSession, actor: Actor, receipt_line_id: uuid.UUID, *,
                  numbers: list[str], scope: set[uuid.UUID] | None) -> dict:
    """Stick QR labels on `len(numbers)` held units: they go into the shop (and Online)."""
    line = await db.get(ReceiptLine, receipt_line_id)
    if line is None or not line.hold:
        raise NotFound("held goods not found")
    batch = await db.get(StockBatch, line.batch_id, with_for_update=True,
                         populate_existing=True)
    if scope is not None and batch.branch_id not in scope:
        raise NotFound("held goods not found")
    product = await db.get(Product, batch.product_id)
    if product.unit == "kg":
        raise SupplyError(f"{product.name} is kept by weight: it cannot be held")
    if not numbers:
        raise SupplyError("scan the QR label of each unit taken in")
    # serialise releases of one line: the batch row lock above
    left = _left(line, batch, (await _released(db, [line.receipt_line_id])).get(
        line.receipt_line_id, 0))
    if len(numbers) > left:
        raise SupplyError(f"{product.name}: {len(numbers)} labels but only {left} held "
                          "units are left")
    from app.services.receiving import ReceiptLineIn, _labels_free_of_drafts  # noqa: PLC0415
    await _labels_free_of_drafts(
        db, [ReceiptLineIn(order_line_id=None, units=1, labels=tuple(numbers))], None)
    linked = await labels.link(db, actor, batch=batch, numbers=numbers, held_release=True)
    row = StockRelease(release_id=uuid.uuid4(), receipt_line_id=line.receipt_line_id,
                       batch_id=batch.batch_id, branch_id=batch.branch_id,
                       units=len(linked.linked), created_by=actor.user_id,
                       send_status="pending" if get_settings().online_stock_in else "off")
    db.add(row)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="held.release", entity="stock_batch",
                       entity_id=batch.batch_id,
                       after={"batch_no": batch.batch_no, "units": row.units,
                              "left": left - row.units, "release_id": str(row.release_id)},
                       ip_address=actor.ip_address)
    return {"release_id": row.release_id, "units": row.units, "left": left - row.units,
            "labels_on_batch": linked.labels_on_batch, "online": row.send_status}


# ---------------------------------------------------------------- Online (worker)
async def _send_one(db: AsyncSession, client: httpx.AsyncClient, release_id: uuid.UUID) -> str:
    rel = await db.get(StockRelease, release_id, with_for_update=True, populate_existing=True)
    if rel is None or rel.send_status != "pending":
        return "skipped"
    line = await db.get(ReceiptLine, rel.receipt_line_id)
    receipt = await db.get(Receipt, line.receipt_id)
    branch = await db.get(Branch, rel.branch_id)
    batch = await db.get(StockBatch, rel.batch_id)
    variant = await db.get(OnlineVariant, line.product_id)
    rel.send_attempts += 1
    rel.updated_at = datetime.now(UTC)
    why = None
    if branch.online_branch_id is None:
        why = f"branch {branch.name} is not linked to a SRIBEESonline branch"
    elif variant is None:
        why = "this product is not an Online pack size"
    if why:
        rel.send_status, rel.send_reason = "refused", why[:300]
        return "refused"
    arrived = online_stock.Arrived(
        line_id=rel.release_id, online_product_id=variant.online_product_id,
        online_variant_id=variant.online_variant_id, units=rel.units, free_units=0,
        value=(batch.unit_cost * rel.units), listed_price=line.listed_price,
        selling_price=line.selling_price, expiry_date=line.expiry_date, mrp=line.mrp)
    try:
        (answer,) = await online_stock.send(client, branch.online_branch_id,
                                            f"{receipt.receipt_no}-H", [arrived])
    except SupplyError as exc:  # nothing known to be taken: try again next round
        rel.send_reason = str(exc)[:300]
        return "waiting"
    rel.send_status = "sent" if answer.sent else "refused"
    rel.send_reason = answer.reason
    if answer.batch is not None and await db.get(PriceReview, line.receipt_line_id) is None:
        # the review belongs to the received line (one per line, the first release makes it)
        await price_reviews.note(db, receipt, replace(arrived, line_id=line.receipt_line_id),
                                 answer.batch)
    return rel.send_status


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient) -> dict:
    if not online_stock.enabled():
        return {}
    async with sessionmaker() as db:
        ids = list(await db.scalars(
            select(StockRelease.release_id).where(StockRelease.send_status == "pending")
            .order_by(StockRelease.send_attempts, StockRelease.created_at).limit(ROUND)))
    totals: dict[str, int] = {}
    for release_id in ids:
        async with sessionmaker() as db, db.begin():
            status = await _send_one(db, client, release_id)
        totals[status] = totals.get(status, 0) + 1
    return totals


async def retry(db: AsyncSession, actor: Actor, release_id: uuid.UUID,
                scope: set[uuid.UUID] | None) -> None:
    rel = await db.get(StockRelease, release_id)
    if rel is None or (scope is not None and rel.branch_id not in scope):
        raise NotFound("release not found")
    done = await db.execute(update(StockRelease).where(
        StockRelease.release_id == release_id, StockRelease.send_status == "refused")
        .values(send_status="pending", send_reason=None))
    if not done.rowcount:
        raise Conflict("this release was not refused")
    await audit.record(db, user_id=actor.user_id, action="held.release_retry",
                       entity="stock_release", entity_id=release_id, after={})


async def releases(db: AsyncSession, scope: set[uuid.UUID] | None, limit: int = 50) -> list[dict]:
    """Recent releases with their Online state (newest first)."""
    q = (select(StockRelease, Product.name, StockBatch.batch_no)
         .join(StockBatch, StockBatch.batch_id == StockRelease.batch_id)
         .join(Product, Product.product_id == StockBatch.product_id)
         .order_by(StockRelease.created_at.desc()).limit(limit))
    if scope is not None:
        q = q.where(StockRelease.branch_id.in_(scope))
    return [{"release_id": r.release_id, "name": name, "batch_no": no, "units": r.units,
             "online": r.send_status, "reason": r.send_reason, "created_at": r.created_at}
            for r, name, no in (await db.execute(q)).all()]
