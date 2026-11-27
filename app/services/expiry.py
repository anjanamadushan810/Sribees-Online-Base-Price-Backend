"""Goods close to expiry (owner plan 2026-11-27, P7).

A batch with stock is "near" from its product's alert days before the expiry date (set in
SRIBEES Online: product, else sub-category, else category; nothing set = 30) and "expired"
after it. Expired stock cannot be packed (picks.py refuses its QR); staff write it off from
this list with the normal write-off. Dates are Sri Lanka dates.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, Label, OnlineProduct, OnlineVariant, Product, StockBatch
from app.services.drafts import colombo_today

DEFAULT_DAYS = 30
LOOK_AHEAD = 365


async def expiring(db: AsyncSession, scope: set[uuid.UUID] | None,
                   today: date | None = None) -> dict:
    today = today or colombo_today()
    q = (select(StockBatch, Product, OnlineProduct.expiry_alert_days, OnlineProduct.image_url,
                OnlineProduct.name_si, Branch.name)
         .join(Product, Product.product_id == StockBatch.product_id)
         .outerjoin(OnlineVariant, OnlineVariant.online_variant_id == StockBatch.product_id)
         .outerjoin(OnlineProduct,
                    OnlineProduct.online_product_id == OnlineVariant.online_product_id)
         .join(Branch, Branch.branch_id == StockBatch.branch_id)
         .where(StockBatch.qty_on_hand > 0, StockBatch.expiry_date.is_not(None),
                StockBatch.status == "available",
                StockBatch.expiry_date <= today + timedelta(days=LOOK_AHEAD))
         .order_by(StockBatch.expiry_date, Product.name))
    if scope is not None:
        q = q.where(StockBatch.branch_id.in_(scope))
    rows = (await db.execute(q)).all()
    ids = [b.batch_id for b, *_ in rows]
    labelled = dict((await db.execute(
        select(Label.batch_id, func.count()).where(Label.batch_id.in_(ids),
                                                   Label.status == "linked")
        .group_by(Label.batch_id))).all()) if ids else {}
    items = []
    for batch, product, days, image, name_si, branch_name in rows:
        days = days or DEFAULT_DAYS
        left = (batch.expiry_date - today).days
        if left > days:
            continue
        items.append({
            "batch_id": batch.batch_id, "batch_no": batch.batch_no,
            "branch_id": batch.branch_id, "branch_name": branch_name,
            "product_id": product.product_id, "name": product.name, "name_si": name_si,
            "image_url": image, "unit": product.unit,
            "expiry_date": batch.expiry_date, "days_left": left, "alert_days": days,
            "qty_on_hand": f"{batch.qty_on_hand.normalize():f}",
            "labels_on_shelf": labelled.get(batch.batch_id, 0),
            "state": "expired" if left < 0 else "near",
        })
    return {"today": today, "items": items,
            "expired": sum(1 for i in items if i["state"] == "expired"),
            "near": sum(1 for i in items if i["state"] == "near")}
