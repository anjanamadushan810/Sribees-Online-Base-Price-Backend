"""Numbers for the Home page (UI plan, section 6). Read-only; every figure comes from the
same tables the pages use, so a card and the list behind it always agree.

  today              what this person has to do now (counts that link to a filtered list)
  orders_summary     orders by stage, and the last seven days' value
  price_changes      supplier prices that moved in the last N days
  base_price_insight what we last paid against the cheapest offer we could take today
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    BasePrice,
    OfferMatch,
    OnlineProduct,
    Supplier,
    SupplierOffer,
    SupplierProduct,
    SupplyOrder,
    SupplyOrderLine,
)
from app.services.orders import StaffActor

STALE_HOURS = 24


def _now() -> datetime:
    return datetime.now(UTC)


def _visible(q, actor: StaffActor):
    """Orders the person may see (their branches)."""
    if actor.branch_scope is not None:
        import uuid
        q = q.where(SupplyOrder.branch_id.in_(actor.branch_scope or {uuid.uuid4()}))
    return q


async def _count(db: AsyncSession, q) -> int:
    return int(await db.scalar(select(func.count()).select_from(q.subquery())) or 0)


def _active_offers():
    """Offers a buyer could order: active, not held, on an active item of an active supplier."""
    return (
        select(SupplierOffer.offer_id)
        .join(SupplierProduct, SupplierProduct.product_id == SupplierOffer.product_id)
        .join(Supplier, Supplier.supplier_id == SupplierOffer.supplier_id)
        .where(SupplierOffer.is_active.is_(True), SupplierOffer.held_reason.is_(None),
               SupplierProduct.is_active.is_(True), Supplier.is_active.is_(True))
    )


async def today(db: AsyncSession, actor: StaffActor) -> dict:
    """Counts of what needs a person. Rows the role cannot act on are left out."""
    now = _now()
    stale = now - timedelta(hours=STALE_HOURS)
    mode = get_settings().order_approval
    rows: list[dict] = []

    def order_q(*where):
        return _visible(select(SupplyOrder.order_id).where(*where), actor)

    if actor.can("orders.read"):
        waiting = await _count(db, order_q(SupplyOrder.status == "submitted"))
        if actor.can("orders.approve") or mode == "finance":
            rows.append({"key": "approve", "count": waiting,
                         "route": "/orders?status=submitted", "in_finance": mode == "finance"})
        rows.append({"key": "unopened", "route": "/orders?status=approved", "count": await _count(
            db, order_q(SupplyOrder.status == "approved", SupplyOrder.supplier_seen_at.is_(None),
                        SupplyOrder.approved_at < stale))})
        rows.append({"key": "declined", "route": "/orders?status=declined", "count": await _count(
            db, order_q(SupplyOrder.status.in_(("declined", "rejected")),
                        func.coalesce(SupplyOrder.answered_at, SupplyOrder.rejected_at)
                        >= now - timedelta(days=7)))})
        if actor.can("orders.create"):
            rows.append({"key": "drafts", "route": "/orders?status=draft", "count": await _count(
                db, order_q(SupplyOrder.status == "draft"))})

    if actor.can("matches.write"):
        matched = exists().where(OfferMatch.offer_id == SupplierOffer.offer_id,
                                 OfferMatch.status == "confirmed")
        q = _active_offers().where(~matched)
        rows.append({"key": "to_match", "route": "/matching", "count": await _count(db, q)})

    if actor.can("suppliers.read"):
        held = (select(SupplierOffer.offer_id).join(
            Supplier, Supplier.supplier_id == SupplierOffer.supplier_id)
            .where(SupplierOffer.is_active.is_(True), SupplierOffer.held_reason.is_not(None),
                   Supplier.is_active.is_(True)))
        rows.append({"key": "held", "route": "/held", "count": await _count(db, held)})

    return {"approval_mode": mode, "rows": rows,
            "open_total": sum(r["count"] for r in rows if r["key"] != "drafts")}


async def orders_summary(db: AsyncSession, actor: StaffActor) -> dict:
    now = _now()
    base = _visible(select(SupplyOrder.status, SupplyOrder.supplier_seen_at), actor)
    stages = {"draft": 0, "waiting_approval": 0, "sent": 0, "opened": 0, "accepted": 0,
              "declined": 0, "rejected": 0, "received": 0}
    for status, seen in (await db.execute(base)).all():
        if status == "submitted":
            stages["waiting_approval"] += 1
        elif status == "approved":
            stages["opened" if seen else "sent"] += 1
        elif status in stages:
            stages[status] += 1
    since = now - timedelta(days=7)
    week = _visible(
        select(func.count(func.distinct(SupplyOrder.order_id)),
               func.coalesce(func.sum(SupplyOrderLine.line_total), 0))
        .select_from(SupplyOrder)
        .join(SupplyOrderLine, SupplyOrderLine.order_id == SupplyOrder.order_id)
        .where(SupplyOrder.submitted_at >= since,
               SupplyOrder.status.notin_(("draft", "cancelled", "rejected", "declined"))),
        actor)
    n, value = (await db.execute(week)).one()
    return {"stages": stages, "week_orders": int(n), "week_value": f"{Decimal(value):.2f}"}


async def price_changes(db: AsyncSession, days: int = 7, limit: int = 50) -> dict:
    """Supplier price lines whose price moved in the last `days` days, newest first."""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 200))
    since = _now() - timedelta(days=days)
    rows = (await db.execute(text("""
        WITH h AS (
            SELECT history_id, offer_id, supplier_id, price, changed_at,
                   lag(price) OVER (PARTITION BY offer_id ORDER BY changed_at, history_id) AS prev
              FROM supplier_offer_prices
        )
        SELECT h.offer_id, h.supplier_id, s.name AS supplier_name, p.heading, o.label,
               h.prev AS old_price, h.price AS new_price, h.changed_at
          FROM h
          JOIN supplier_offers o ON o.offer_id = h.offer_id
          JOIN supplier_products p ON p.product_id = o.product_id
          JOIN suppliers s ON s.supplier_id = h.supplier_id
         WHERE h.changed_at >= :since AND h.prev IS NOT NULL AND h.price <> h.prev
         ORDER BY h.changed_at DESC, h.history_id DESC
         LIMIT :limit
    """), {"since": since, "limit": limit})).mappings().all()
    items = [{**dict(r), "offer_id": str(r["offer_id"]), "supplier_id": str(r["supplier_id"]),
              "old_price": f"{r['old_price']:.2f}", "new_price": f"{r['new_price']:.2f}",
              "changed_at": r["changed_at"]} for r in rows]
    up = sum(1 for r in rows if r["new_price"] > r["old_price"])
    return {"days": days, "items": items, "increases": up, "decreases": len(rows) - up}


async def base_price_insight(db: AsyncSession, limit: int = 20) -> dict:
    """For every product we have paid for: what we last paid, and the cheapest offer we
    could take today (confirmed match, active, not held). `never_ordered` are products with
    an offer on the table and no base price yet."""
    limit = max(1, min(limit, 100))
    cheapest = (
        select(
            OfferMatch.online_product_id.label("pid"),
            OfferMatch.online_variant_id.label("vid"),
            SupplierOffer.price.label("price"),
            SupplierOffer.supplier_id.label("sid"),
            func.row_number().over(
                partition_by=(OfferMatch.online_product_id, OfferMatch.online_variant_id),
                order_by=SupplierOffer.price).label("rn"),
        )
        .select_from(SupplierOffer)
        .join(OfferMatch, and_(OfferMatch.offer_id == SupplierOffer.offer_id,
                               OfferMatch.status == "confirmed"))
        .where(SupplierOffer.offer_id.in_(_active_offers()))
    ).subquery()
    best = select(cheapest).where(cheapest.c.rn == 1).subquery()

    latest = (select(BasePrice).where(BasePrice.voided_at.is_(None))
              .order_by(BasePrice.online_product_id, BasePrice.online_variant_id,
                        BasePrice.recorded_at.desc(), BasePrice.base_price_id.desc())
              .distinct(BasePrice.online_product_id, BasePrice.online_variant_id)).subquery()
    base_s, best_s = Supplier.__table__.alias("bs"), Supplier.__table__.alias("cs")
    q = (select(latest.c.online_product_id, OnlineProduct.name, latest.c.unit_price,
                base_s.c.name.label("base_supplier"), best.c.price.label("best_price"),
                best_s.c.name.label("best_supplier"), latest.c.recorded_at)
         .select_from(latest)
         .join(best, and_(best.c.pid == latest.c.online_product_id,
                          best.c.vid.is_not_distinct_from(latest.c.online_variant_id)))
         .join(OnlineProduct, OnlineProduct.online_product_id == latest.c.online_product_id)
         .join(base_s, base_s.c.supplier_id == latest.c.supplier_id)
         .join(best_s, best_s.c.supplier_id == best.c.sid))
    items = []
    for r in (await db.execute(q)).all():
        saving = r.unit_price - r.best_price
        items.append({
            "online_product_id": str(r.online_product_id), "product_name": r.name,
            "base_price": f"{r.unit_price:.2f}", "base_supplier": r.base_supplier,
            "best_price": f"{r.best_price:.2f}", "best_supplier": r.best_supplier,
            "saving": f"{saving:.2f}",
            "saving_pct": round(float(saving / r.unit_price * 100), 1) if r.unit_price else 0.0,
            "last_paid_at": r.recorded_at,
        })
    can_save = sorted((i for i in items if Decimal(i["saving"]) > 0),
                      key=lambda i: i["saving_pct"], reverse=True)
    paid = select(latest.c.online_product_id)
    never = (select(OnlineProduct.online_product_id, OnlineProduct.name)
             .where(OnlineProduct.online_product_id.in_(select(best.c.pid)),
                    OnlineProduct.online_product_id.notin_(paid))
             .order_by(OnlineProduct.name))
    never_rows = (await db.execute(never)).all()
    return {"products_compared": len(items), "can_save": can_save[:limit],
            "can_save_count": len(can_save),
            "never_ordered_count": len(never_rows),
            "never_ordered": [{"online_product_id": str(i), "product_name": n}
                              for i, n in never_rows[:10]]}
