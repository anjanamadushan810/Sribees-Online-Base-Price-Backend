"""Fulfilment partner, plan P5: the weekly statement (owner MCQ 2026-11-28, F19).

A week is Monday to Sunday (Sri Lanka). Its statement is what Supply knows:

- each day's partner order (one Finance PO per day, F18): date, order and PO number, the
  receipt and its total;
- goods the partner took back that were already bought (accepted returns): their credit;
- to pay = purchases - credits.

The partner sees the same page in its app and may confirm it. The money itself stays in
Finance: each day's receipt became the partner's invoice there, each credit a supplier credit,
and the OWNER approves the payment in Finance's payments (request -> owner approval -> paid),
which settles those invoices and uses those credits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    FulfilmentRequest,
    PartnerReturn,
    PartnerStatementConfirm,
    Receipt,
    Supplier,
    SupplierBranch,
    SupplyOrder,
)
from app.services import audit, orders
from app.services.errors import Forbidden, NotFound, SupplyError
from app.services.partners import money

COLOMBO = orders.COLOMBO
ZERO = Decimal("0")


def week_of(day: date) -> date:
    """The Monday that starts the week of `day`."""
    return day - timedelta(days=day.weekday())


def _this_week() -> date:
    return week_of(datetime.now(UTC).astimezone(COLOMBO).date())


def _bounds(week: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week, time.min, tzinfo=COLOMBO)
    return start.astimezone(UTC), (start + timedelta(days=7)).astimezone(UTC)


async def statement(db: AsyncSession, supplier_id: uuid.UUID, branch_id: uuid.UUID,
                    week: date) -> dict:
    if week.weekday() != 0:
        raise SupplyError("a statement week starts on a Monday")
    end_day = week + timedelta(days=6)
    from app.services.partner_books import _partner_receipt_ids  # noqa: PLC0415

    rows = (await db.execute(
        select(SupplyOrder, Receipt).join(Receipt, Receipt.order_id == SupplyOrder.order_id)
        .where(Receipt.receipt_id.in_(_partner_receipt_ids(supplier_id, branch_id)),
               Receipt.received_date >= week, Receipt.received_date <= end_day)
        .order_by(Receipt.received_date, SupplyOrder.order_no))).all()
    counts = dict((await db.execute(
        select(FulfilmentRequest.supply_order_id, func.count())
        .where(FulfilmentRequest.supply_order_id.in_([o.order_id for o, _ in rows]))
        .group_by(FulfilmentRequest.supply_order_id))).all()) if rows else {}
    days = [{"date": r.received_date.isoformat(), "order_no": o.order_no,
             "finance_po_no": o.finance_po_no, "receipt_no": r.receipt_no,
             "customer_orders": counts.get(o.order_id, 0), "total": money(r.total)}
            for o, r in rows]
    start, end = _bounds(week)
    rets = list(await db.scalars(select(PartnerReturn).where(
        PartnerReturn.supplier_id == supplier_id, PartnerReturn.branch_id == branch_id,
        PartnerReturn.status == "accepted", PartnerReturn.in_books.is_(True),
        PartnerReturn.accepted_at >= start, PartnerReturn.accepted_at < end)
        .order_by(PartnerReturn.accepted_at)))
    credits = [{"date": r.accepted_at.astimezone(COLOMBO).date().isoformat(),
                "return_no": r.return_no, "reason": r.reason,
                "total": money(r.credit_total or ZERO)} for r in rets]
    purchases = sum((r.total for _, r in rows), ZERO)
    returned = sum((r.credit_total or ZERO for r in rets), ZERO)
    to_pay = purchases - returned
    confirm = await db.get(PartnerStatementConfirm, (supplier_id, branch_id, week))
    supplier = await db.get(Supplier, supplier_id)
    return {"supplier_id": str(supplier_id), "partner_name": supplier.name if supplier else None,
            "branch_id": str(branch_id), "week_start": week.isoformat(),
            "week_end": end_day.isoformat(), "closed": end_day < _this_week(),
            "days": days, "returns": credits, "purchases": money(purchases),
            "returns_total": money(returned), "to_pay": money(to_pay),
            "confirmed_at": confirm.confirmed_at if confirm else None,
            "confirmed_total": money(confirm.total) if confirm else None,
            "changed_since_confirmed": bool(confirm and confirm.total != to_pay),
            "payment": "the owner approves this payment in Sribees Finance (Payments)"}


async def weeks(db: AsyncSession, supplier_id: uuid.UUID, branch_id: uuid.UUID,
                count: int = 8) -> list[dict]:
    """The last `count` weeks' totals, newest first."""
    out = []
    week = _this_week()
    for _ in range(max(1, min(count, 52))):
        s = await statement(db, supplier_id, branch_id, week)
        out.append({k: s[k] for k in ("week_start", "week_end", "closed", "purchases",
                                      "returns_total", "to_pay", "confirmed_at",
                                      "changed_since_confirmed")})
        week -= timedelta(days=7)
    return out


async def _link(db: AsyncSession, supplier_id: uuid.UUID | None,
                branch_id: uuid.UUID) -> uuid.UUID:
    if supplier_id is not None:
        return supplier_id
    found = await db.scalar(select(SupplierBranch.supplier_id)
                            .where(SupplierBranch.branch_id == branch_id))
    if found is None:
        # an unlinked partner's past weeks: the last one that served the branch
        found = await db.scalar(select(FulfilmentRequest.supplier_id)
                                .where(FulfilmentRequest.branch_id == branch_id)
                                .order_by(FulfilmentRequest.created_at.desc()).limit(1))
    if found is None:
        raise NotFound("this branch has no fulfilment partner")
    return found


async def staff_view(db: AsyncSession, scope: set[uuid.UUID] | None, *, branch_id: uuid.UUID,
                     supplier_id: uuid.UUID | None = None, week: date | None = None) -> dict:
    if scope is not None and branch_id not in scope:
        raise Forbidden("you do not have access to this branch")
    supplier_id = await _link(db, supplier_id, branch_id)
    if week is not None:
        return await statement(db, supplier_id, branch_id, week_of(week))
    return {"supplier_id": str(supplier_id), "branch_id": str(branch_id),
            "weeks": await weeks(db, supplier_id, branch_id)}


async def _partner_branch(db, supplier_id: uuid.UUID, branch_id: uuid.UUID | None) -> uuid.UUID:
    from app.services.partners import my_links  # noqa: PLC0415

    links = await my_links(db, supplier_id)
    if branch_id is None:
        if len(links) == 1:
            return next(iter(links))
        served = await db.scalar(select(FulfilmentRequest.branch_id)
                                 .where(FulfilmentRequest.supplier_id == supplier_id)
                                 .order_by(FulfilmentRequest.created_at.desc()).limit(1))
        if served is None:
            raise NotFound("no statement yet")
        return served
    if branch_id not in links and not await db.scalar(
            select(FulfilmentRequest.request_id).where(
                FulfilmentRequest.supplier_id == supplier_id,
                FulfilmentRequest.branch_id == branch_id).limit(1)):
        raise Forbidden("you do not serve this branch")
    return branch_id


async def partner_view(db: AsyncSession, supplier_id: uuid.UUID, *,
                       branch_id: uuid.UUID | None = None, week: date | None = None) -> dict:
    branch_id = await _partner_branch(db, supplier_id, branch_id)
    if week is not None:
        return await statement(db, supplier_id, branch_id, week_of(week))
    return {"branch_id": str(branch_id), "weeks": await weeks(db, supplier_id, branch_id)}


async def confirm(db: AsyncSession, supplier_id: uuid.UUID, supplier_user_id: uuid.UUID, *,
                  week: date, branch_id: uuid.UUID | None = None) -> dict:
    """The partner agrees with a finished week's statement (its total at that moment)."""
    branch_id = await _partner_branch(db, supplier_id, branch_id)
    week = week_of(week)
    s = await statement(db, supplier_id, branch_id, week)
    if not s["closed"]:
        raise SupplyError("a week is confirmed after it ends (Sunday night)")
    total = Decimal(s["to_pay"])
    row = await db.get(PartnerStatementConfirm, (supplier_id, branch_id, week),
                       with_for_update=True)
    if row is None:
        db.add(PartnerStatementConfirm(supplier_id=supplier_id, branch_id=branch_id,
                                       week_start=week, total=total,
                                       confirmed_by=supplier_user_id))
    else:
        row.total, row.confirmed_by = total, supplier_user_id
        row.confirmed_at = datetime.now(UTC)
    await audit.record(db, supplier_user_id=supplier_user_id, action="partner.statement_confirm",
                       entity="partner_statement", entity_id=f"{branch_id}:{week}",
                       after={"to_pay": str(total)})
    await db.flush()
    return await statement(db, supplier_id, branch_id, week)
