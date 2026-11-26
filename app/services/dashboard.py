"""Numbers for the Home page (UI plan, section 6). Read-only; every figure comes from the
same tables the pages use, so a card and the list behind it always agree.

  today              what this person has to do now (counts that link to a filtered list)
  orders_summary     orders by stage, and the last seven days' value
  price_changes      supplier prices that moved in the last N days
  base_price_insight what we last paid against the cheapest offer we could take today
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    BasePrice,
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

    if actor.can("base_prices.read"):
        rows.append({"key": "price_jumps", "route": "/", "count": await price_jumps(db)})

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


JUMP = Decimal("1.10")  # a bill price more than 10% above the last one is flagged (owner)

_BILL_MOVES = """
    WITH h AS (
        SELECT b.base_price_id, b.online_product_id, b.online_variant_id, b.supplier_id,
               b.unit_price, b.recorded_at,
               lag(b.unit_price) OVER (PARTITION BY b.online_variant_id
                                       ORDER BY b.recorded_at, b.base_price_id) AS prev
          FROM base_prices b
         WHERE b.voided_at IS NULL
    )
    SELECT h.base_price_id, h.supplier_id, s.name AS supplier_name, p.name AS heading,
           COALESCE(v.name, '') AS label, h.prev AS old_price, h.unit_price AS new_price,
           h.recorded_at AS changed_at
      FROM h
      JOIN suppliers s ON s.supplier_id = h.supplier_id
      JOIN online_products p ON p.online_product_id = h.online_product_id
      LEFT JOIN online_variants v ON v.online_variant_id = h.online_variant_id
     WHERE h.recorded_at >= :since AND h.prev IS NOT NULL AND h.unit_price <> h.prev
"""


async def price_jumps(db: AsyncSession, days: int = 7) -> int:
    """Bill prices more than 10% above the last price paid for the same item."""
    since = _now() - timedelta(days=days)
    return int(await db.scalar(text(
        # a fixed query of our own; every value is a bound parameter
        f"SELECT count(*) FROM ({_BILL_MOVES}) m WHERE m.new_price > m.old_price * :jump"),  # noqa: S608
        {"since": since, "jump": JUMP}) or 0)


async def price_changes(db: AsyncSession, days: int = 7, limit: int = 50) -> dict:
    """What we paid that moved in the last `days` days, newest first (owner 2026-11-26: the
    bill prices staff enter at receiving; supplier price lists are no longer linked)."""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 200))
    since = _now() - timedelta(days=days)
    rows = (await db.execute(text(
        _BILL_MOVES + " ORDER BY h.recorded_at DESC, h.base_price_id DESC LIMIT :limit"),
        {"since": since, "limit": limit})).mappings().all()
    items = [{"id": str(r["base_price_id"]), "supplier_id": str(r["supplier_id"]),
              "supplier_name": r["supplier_name"], "heading": r["heading"],
              "label": r["label"] if r["label"].lower() not in r["heading"].lower() else "",
              "old_price": f"{r['old_price']:.2f}", "new_price": f"{r['new_price']:.2f}",
              "jump": r["new_price"] > r["old_price"] * JUMP,
              "changed_at": r["changed_at"]} for r in rows]
    up = sum(1 for r in rows if r["new_price"] > r["old_price"])
    return {"days": days, "items": items, "increases": up, "decreases": len(rows) - up}


async def base_price_insight(db: AsyncSession, limit: int = 20) -> dict:
    """For every product we have paid for: what we last paid, and the cheapest that any
    supplier last sold it to us for (owner 2026-11-26: supplier price lists are no longer
    linked to our products, so we compare what we really paid)."""
    limit = max(1, min(limit, 100))
    per_supplier = (select(BasePrice).where(BasePrice.voided_at.is_(None))
                    .order_by(BasePrice.online_product_id, BasePrice.online_variant_id,
                              BasePrice.supplier_id, BasePrice.recorded_at.desc(),
                              BasePrice.base_price_id.desc())
                    .distinct(BasePrice.online_product_id, BasePrice.online_variant_id,
                              BasePrice.supplier_id)).subquery()
    cheapest = (
        select(per_supplier.c.online_product_id.label("pid"),
               per_supplier.c.online_variant_id.label("vid"),
               per_supplier.c.unit_price.label("price"),
               per_supplier.c.supplier_id.label("sid"),
               func.row_number().over(
                   partition_by=(per_supplier.c.online_product_id,
                                 per_supplier.c.online_variant_id),
                   order_by=per_supplier.c.unit_price).label("rn"))
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
    never_rows: list = []
    return {"products_compared": len(items), "can_save": can_save[:limit],
            "can_save_count": len(can_save),
            "never_ordered_count": len(never_rows),
            "never_ordered": [{"online_product_id": str(i), "product_name": n}
                              for i, n in never_rows[:10]]}
